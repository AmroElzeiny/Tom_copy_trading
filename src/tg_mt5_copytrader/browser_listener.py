from __future__ import annotations

import asyncio
import logging
import re
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from playwright.async_api import Page, async_playwright

from .config import Settings

log = logging.getLogger(__name__)

# Reads the currently open chat from Telegram Web. Supports both clients:
# Web K (web.telegram.org/k/) and Web A (web.telegram.org/a/).
_SNAPSHOT_JS = r"""
() => {
  const textOf = (el, junk) => {
    if (!el) return '';
    const clone = el.cloneNode(true);
    clone.querySelectorAll(junk).forEach(n => n.remove());
    clone.querySelectorAll('img[alt]').forEach(img => img.replaceWith(document.createTextNode(img.getAttribute('alt'))));
    clone.querySelectorAll('br').forEach(br => br.replaceWith(document.createTextNode('\n')));
    return (clone.textContent || '').replace(/ /g, ' ').trim();
  };
  const out = {client: null, hash: location.hash || '', title: null, messages: []};

  // ---- Web K ----
  const kRoot = document.querySelector('#column-center .chat.active') || document.querySelector('#column-center');
  const kBubbles = kRoot ? kRoot.querySelectorAll('.bubble[data-mid]') : [];
  if (kBubbles.length || document.querySelector('#column-center')) {
    out.client = 'K';
    const t = kRoot && kRoot.querySelector('.chat-info .peer-title, .top .peer-title, .user-title');
    out.title = t ? t.textContent.trim() : null;
    const junk = '.time, .time-inner, .reactions, reactions-element, .reply, .name, .bubble-name-forwarded, .webpage, .message-reactions, .tgico, .inline-keyboard';
    kBubbles.forEach(b => {
      if (b.classList.contains('service')) return;
      const mid = Number(b.dataset.mid);
      if (!Number.isInteger(mid)) return;  // local/pending ids are fractional
      const msg = b.querySelector('.translatable-message') || b.querySelector('.message');
      out.messages.push({
        mid, ts: Number(b.dataset.timestamp) || null,
        out: b.classList.contains('is-out'),
        text: textOf(msg, junk),
      });
    });
    if (out.messages.length) return out;
  }

  // ---- Web A ----
  const aMessages = document.querySelectorAll('.messages-container .Message, .MessageList .Message');
  if (aMessages.length || document.querySelector('.MiddleHeader')) {
    out.client = 'A';
    const t = document.querySelector('.MiddleHeader .fullName, .MiddleHeader .title h3, .ChatInfo .fullName');
    out.title = t ? t.textContent.trim() : null;
    out.messages = [];
    aMessages.forEach(m => {
      const idRaw = m.dataset.messageId || (m.id || '').replace('message-', '');
      const mid = Number(idRaw);
      if (!Number.isInteger(mid) || mid <= 0) return;
      const content = m.querySelector('.text-content');
      out.messages.push({
        mid, ts: null,
        out: m.classList.contains('own'),
        text: textOf(content, '.MessageMeta, .Reactions, .message-time, .EmbeddedMessage'),
      });
    });
  }
  return out;
}
"""

ProcessFn = Callable[..., Awaitable[object]]


@dataclass(slots=True)
class _Pending:
    text: str
    ts: float | None
    out: bool
    stable_polls: int = 0


class BrowserSignalListener:
    """Opens Telegram Web in a real browser window; you log in and open the signal chat.

    The listener then reads the open chat one message after another, in order.
    Messages already visible when a chat is opened are history and never traded.
    """

    def __init__(self, settings: Settings, process_message: ProcessFn | None, *, probe: bool = False) -> None:
        self.settings = settings
        self.process_message = process_message
        self.probe = probe

    async def run(self) -> None:
        profile = self.settings.browser_profile_dir.resolve()
        profile.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                # "chrome"/"msedge" use the installed browser; "chromium" needs `playwright install chromium`.
                channel=None if self.settings.browser_channel in {"", "chromium"} else self.settings.browser_channel,
                headless=False,
                no_viewport=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    # Keep Telegram rendering new messages while the window is minimized/covered.
                    "--disable-background-timer-throttling",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                ],
            )
            page = context.pages[0] if context.pages else await context.new_page()
            if not page.url.startswith("https://web.telegram.org"):
                await page.goto(self.settings.telegram_web_url, wait_until="domcontentloaded")
            log.info(
                "Browser opened. Log in to Telegram Web if asked, then open the signal chat%s and keep it scrolled to the latest message.",
                f" (title containing {self.settings.browser_chat_title!r})" if self.settings.browser_chat_title else "",
            )
            try:
                await self._watch(page)
            finally:
                try:
                    await context.close()
                except Exception:
                    pass

    @staticmethod
    def _chat_id(hash_value: str, title: str | None) -> int:
        peer = hash_value.lstrip("#").split("?")[0]
        if re.fullmatch(r"-?\d+", peer):
            return int(peer)
        return zlib.crc32((peer or title or "unknown").encode("utf-8"))

    async def _watch(self, page: Page) -> None:
        poll = self.settings.browser_poll_ms / 1000.0
        chat_key: str | None = None
        watching = False
        high_water = 0
        pending: dict[int, _Pending] = {}
        waiting_logged = False

        while True:
            if page.is_closed():
                log.warning("Telegram browser window was closed; stopping.")
                return
            try:
                snap = await page.evaluate(_SNAPSHOT_JS)
            except Exception as exc:  # navigation in progress, page reloading, etc.
                log.debug("snapshot failed: %r", exc)
                await asyncio.sleep(poll)
                continue

            title = snap.get("title")
            messages = snap.get("messages") or []
            key = f"{snap.get('hash') or ''}|{title or ''}"
            if not messages or not (snap.get("hash") or title):
                if not waiting_logged:
                    log.info("Waiting for you to log in and open a chat...")
                    waiting_logged = True
                await asyncio.sleep(poll)
                continue

            if key != chat_key:
                chat_key = key
                pending.clear()
                high_water = max(m["mid"] for m in messages)
                wanted = self.settings.browser_chat_title.lower()
                watching = not wanted or wanted in (title or "").lower()
                if watching:
                    log.info("Watching chat %r (Web %s). %d existing messages skipped; waiting for new ones.", title, snap.get("client"), len(messages))
                else:
                    log.info("Open chat %r does not match BROWSER_CHAT_TITLE=%r; not watching.", title, self.settings.browser_chat_title)
                if self.probe:
                    for m in sorted(messages, key=lambda x: x["mid"])[-5:]:
                        log.info("[probe] existing mid=%s out=%s text=%r", m["mid"], m["out"], m["text"])
                await asyncio.sleep(poll)
                continue

            if watching:
                chat_id = self._chat_id(snap.get("hash") or "", title)
                await self._handle_new(messages, pending, chat_id, high_water)
                ready = sorted(mid for mid, p in pending.items() if p.stable_polls >= 1)
                # Process strictly in order: stop at the first message still settling.
                for mid in sorted(pending):
                    if mid not in ready:
                        break
                    item = pending.pop(mid)
                    high_water = max(high_water, mid)
                    await self._dispatch(chat_id, mid, item)
            await asyncio.sleep(poll)

    async def _handle_new(self, messages: list[dict], pending: dict[int, _Pending], chat_id: int, high_water: int) -> None:
        present = {int(m["mid"]) for m in messages}
        for mid in [mid for mid in pending if mid not in present]:  # deleted before it settled
            pending.pop(mid)
        for m in messages:
            mid = int(m["mid"])
            if mid <= high_water:
                continue
            text = m.get("text") or ""
            current = pending.get(mid)
            if current is None:
                pending[mid] = _Pending(text=text, ts=m.get("ts"), out=bool(m.get("out")))
            elif current.text == text:
                current.stable_polls += 1
            else:  # still rendering or edited right away; wait for it to settle
                current.text = text
                current.stable_polls = 0

    async def _dispatch(self, chat_id: int, mid: int, item: _Pending) -> None:
        if item.out and not self.settings.browser_include_outgoing:
            log.info("skipping own outgoing message %s", mid)
            return
        if not item.text.strip():
            return
        if self.probe or self.process_message is None:
            log.info("[probe] new message chat=%s mid=%s text=%r", chat_id, mid, item.text)
            return
        message_dt = datetime.fromtimestamp(item.ts, timezone.utc) if item.ts else datetime.now(timezone.utc)
        log.info("new message %s: %r", mid, item.text[:200])
        try:
            await self.process_message(chat_id=chat_id, message_id=mid, text=item.text, message_dt=message_dt)
        except Exception:
            log.exception("message processing failed")
