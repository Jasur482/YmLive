__version__ = (1, 2, 3)

import json
import random
import string
import logging
import asyncio
import aiohttp
import time
import os

from .. import loader, utils
from yandex_music import ClientAsync
from telethon.tl.types import ChatAdminRights, InputChatUploadedPhoto
from telethon.tl.functions.channels import EditAdminRequest, EditPhotoRequest

async def _raw_get_current_track_ws(token):
    """
    Low-level websocket interaction with Ynison service.
    Returns dict with keys:
      - {"success": False} on failure
      - {"success": False, "paused": True/False} when no track but paused info available
      - {"success": True, "player_state":..., "track_entry":...} when track data available
    """
    device_info = {"app_name": "Chrome", "type": 1}
    ws_proto = {
        "Ynison-Device-Id": "".join(random.choice(string.ascii_lowercase) for _ in range(16)),
        "Ynison-Device-Info": json.dumps(device_info),
    }

    timeout = aiohttp.ClientTimeout(total=15, connect=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # first redirect
            async with session.ws_connect(
                url="wss://ynison.music.yandex.ru/redirector.YnisonRedirectService/GetRedirectToYnison",
                headers={
                    "Sec-WebSocket-Protocol": f"Bearer, v2, {json.dumps(ws_proto)}",
                    "Origin": "http://music.yandex.ru",
                    "Authorization": f"OAuth {token}",
                },
                timeout=10,
            ) as ws:
                recv = await ws.receive()
                try:
                    data = json.loads(recv.data)
                except Exception:
                    logging.warning("raw_get_current_track_ws: couldn't parse first ws response: %s", recv.data)
                    return {"success": False}

            if "redirect_ticket" not in data or "host" not in data:
                logging.info("raw_get_current_track_ws: redirect_ticket/host missing: %s", data)
                return {"success": False}

            new_ws_proto = ws_proto.copy()
            new_ws_proto["Ynison-Redirect-Ticket"] = data["redirect_ticket"]

            to_send = {
                "update_full_state": {
                    "player_state": {
                        "player_queue": {
                            "current_playable_index": -1,
                            "entity_id": "",
                            "entity_type": "VARIOUS",
                            "playable_list": [],
                            "options": {"repeat_mode": "NONE"},
                        },
                        "status": {
                            "duration_ms": 0,
                            "paused": True,
                            "playback_speed": 1,
                            "progress_ms": 0,
                        },
                    },
                    "device": {"info": {"device_id": ws_proto["Ynison-Device-Id"]}},
                }
            }

            async with session.ws_connect(
                url=f"wss://{data['host']}/ynison_state.YnisonStateService/PutYnisonState",
                headers={
                    "Sec-WebSocket-Protocol": f"Bearer, v2, {json.dumps(new_ws_proto)}",
                    "Origin": "http://music.yandex.ru",
                    "Authorization": f"OAuth {token}",
                },
                timeout=10,
                method="GET",
            ) as ws2:
                await ws2.send_str(json.dumps(to_send))
                recv2 = await asyncio.wait_for(ws2.receive(), timeout=10)
                try:
                    ynison = json.loads(recv2.data)
                except Exception:
                    logging.warning("raw_get_current_track_ws: couldn't parse second ws response: %s", recv2.data)
                    return {"success": False}

                player_state = ynison.get("player_state", {})
                player_queue = player_state.get("player_queue", {})
                status = player_state.get("status", {})
                track_index = player_queue.get("current_playable_index", -1)

                if track_index == -1:
                    paused_flag = bool(status.get("paused", True))
                    logging.debug("raw_get_current_track_ws: no track (index=-1), paused=%s", paused_flag)
                    return {"success": False, "paused": paused_flag}

                playable_list = player_queue.get("playable_list", [])
                if not playable_list or track_index >= len(playable_list):
                    paused_flag = bool(status.get("paused", True))
                    logging.debug("raw_get_current_track_ws: playable_list missing or index out of range, paused=%s", paused_flag)
                    return {"success": False, "paused": paused_flag}

                track_entry = playable_list[track_index]
                return {"success": True, "player_state": player_state, "track_entry": track_entry}

    except Exception as e:
        logging.exception("raw_get_current_track_ws exception: %s", e)
        return {"success": False}


class YmLive(loader.Module):
    strings = {
        "name": "YandexMusicLive",
        "_text_token": "Токен аккаунта Яндекс Музыки",
        "_text_id": "ID канала для показа треков (без -100)",
        "_text_idle": "Путь к фото-заглушке (через .setidlepic)",
        "on/off": "YandexMusicLive теперь {}",
        "channel_id_error": "В конфиге не указан ID канала.",
        "setidle_no_reply": "Нужно ответить на фото командой .setidlepic",
        "setidle_ok": "Idle обложка сохранена: {}",
    }

    def __init__(self):
        self.config = loader.ModuleConfig(
            loader.ConfigValue("YandexMusicToken", None, lambda: self.strings["_text_token"], validator=loader.validators.Hidden()),
            loader.ConfigValue("channel_id", None, lambda: self.strings["_text_id"], validator=loader.validators.TelegramID()),
            loader.ConfigValue("IdleCoverPath", "hikka_downloads/idle_cover.jpg", lambda: self.strings["_text_idle"]),
        )
        self._last_track_title = None
        self._last_change_ts = 0

    async def client_ready(self, client, db):
        self._client = client
        self.db = db

    async def _normalize_channel_peer(self):
        raw = str(self.config["channel_id"])
        if raw.startswith("-100"):
            try:
                return int(raw)
            except Exception:
                return None
        raw = raw.strip().lstrip("+")
        try:
            return int(f"-100{raw}")
        except Exception:
            try:
                return int(raw)
            except Exception:
                return None

    async def add_bot_to_channel(self, channel_id):
        try:
            await self._client(
                EditAdminRequest(
                    channel=int(channel_id),
                    user_id=self.inline.bot_username,
                    admin_rights=ChatAdminRights(change_info=True),
                    rank="YandexMusicLiveBot"
                )
            )
            self.set("ymlive_bot_added", True)
        except Exception as e:
            logging.info("add_bot_to_channel: %s", e)

    async def get_current_track(self, token):
        try:
            client = ClientAsync(token)
            await client.init()
            raw = await _raw_get_current_track_ws(token)
            if not raw or not raw.get("success"):
                paused_flag = bool(raw.get("paused")) if isinstance(raw, dict) else False
                await client.stop()
                if raw and "paused" in raw:
                    return {"paused": paused_flag}
                return None

            track_entry = raw.get("track_entry")
            if not track_entry:
                await client.stop()
                return None

            playable_id = track_entry.get("playable_id")
            if not playable_id:
                await client.stop()
                return None

            info = await client.tracks_download_info(playable_id, True)
            track = await client.tracks(playable_id)
            await client.stop()

            if isinstance(track, (list, tuple)) and track:
                track_item = track[0]
            else:
                track_item = track

            if not isinstance(track_item, dict):
                logging.warning("get_current_track: unexpected track format: %s", type(track_item))
                return None

            title = track_item.get("title") or ""
            artists = [a.get("name") for a in track_item.get("artists", [])] if track_item.get("artists") else []
            cover_uri = track_item.get("cover_uri") or track_item.get("cover") or None
            paused_flag = bool(raw.get("player_state", {}).get("status", {}).get("paused", False))

            return {"title": title, "artists": artists, "cover_uri": cover_uri, "paused": paused_flag}

        except Exception as e:
            logging.exception("get_current_track exception: %s", e)
            return None

    async def update_channel_title(self, peer, title):
        try:
            await self.inline.bot.set_chat_title(int(peer), title)
        except Exception as e:
            logging.warning("update_channel_title failed: %s", e)

    async def edit_or_send_one_message(self, peer, text):
        try:
            saved_id = self.get("channel_msg_id")
            if saved_id:
                try:
                    await self._client.edit_message(peer, saved_id, text)
                    return
                except Exception:
                    logging.info("edit_or_send_one_message: saved id not editable")
            try:
                msgs = await self._client.get_messages(peer, limit=1)
                if msgs and len(msgs) > 0:
                    try:
                        await msgs[0].edit(text)
                        self.set("channel_msg_id", msgs[0].id)
                        return
                    except Exception:
                        pass
            except Exception:
                logging.debug("edit_or_send_one_message: couldn't fetch recent messages")
            msg = await self._client.send_message(peer, text)
            self.set("channel_msg_id", msg.id)
        except Exception as e:
            logging.exception("edit_or_send_one_message: %s", e)

    async def set_channel_photo_from_path(self, peer, path):
        if not path or not os.path.exists(path):
            logging.debug("set_channel_photo_from_path: file not found: %s", path)
            return
        try:
            uploaded = await self._client.upload_file(path)
            await self._client(EditPhotoRequest(channel=int(peer), photo=InputChatUploadedPhoto(uploaded)))
        except Exception as e:
            logging.exception("set_channel_photo_from_path: %s", e)

    @loader.command(ru_doc="- включить/выключить YaLive")
    async def yalive(self, message):
        if not self.config["channel_id"]:
            await utils.answer(message, self.strings["channel_id_error"])
            return
        peer = await self._normalize_channel_peer()
        if peer:
            await self.add_bot_to_channel(peer)
        autochannel_status = self.get("autochannel", False)
        self.set("autochannel", not autochannel_status)
        status_text = "enabled" if not autochannel_status else "disabled"
        await utils.answer(message, self.strings["on/off"].format(status_text))

    @loader.command(ru_doc="Сохранить фото-заглушку. Использование: .setidlepic (в ответе на фото)")
    async def setidlepiccmd(self, message):
        reply = await message.get_reply_message()
        if not reply or not reply.photo:
            await utils.answer(message, self.strings["setidle_no_reply"])
            return
        path = "hikka_downloads/idle_cover.jpg"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            await self._client.download_media(reply.photo, path)
            self.config["IdleCoverPath"] = path
            await utils.answer(message, self.strings["setidle_ok"].format(path))
        except Exception as e:
            logging.exception("setidlepiccmd failed: %s", e)
            await utils.answer(message, "<b>Ошибка при сохранении фото.</b>")

    @loader.loop(interval=30, autostart=True)
    async def autochannel_loop(self):
        if not self.get("autochannel"):
            return
        if not self.config["channel_id"] or not self.config["YandexMusicToken"]:
            logging.debug("autochannel_loop: channel_id or token missing")
            return
        peer = await self._normalize_channel_peer()
        if not peer:
            logging.warning("autochannel_loop: can't normalize channel id")
            return
        try:
            track_info = await self.get_current_track(self.config["YandexMusicToken"])
            now = time.time()
            if isinstance(track_info, dict) and track_info.get("paused") is True:
                await self.update_channel_title(peer, "⏸️Сейчас ничего не играет")
                await self.edit_or_send_one_message(peer, "-")
                await self.set_channel_photo_from_path(peer, self.config["IdleCoverPath"])
                self._last_track_title = None
                self._last_change_ts = now
                return
            if isinstance(track_info, dict) and track_info.get("title"):
                title = track_info.get("title") or "-"
                artists = ", ".join(track_info.get("artists", [])) or "-"
                cover_uri = track_info.get("cover_uri")
                if title != self._last_track_title:
                    await self.update_channel_title(peer, title)
                    await self.edit_or_send_one_message(peer, artists)
                    if cover_uri:
                        url = cover_uri
                        if url.startswith("//"):
                            url = "https:" + url
                        elif not url.startswith("http"):
                            url = "https://" + url
                        url = url.replace("%%", "400x400")
                        cover_path = "hikka_downloads/current_cover.jpg"
                        try:
                            async with aiohttp.ClientSession() as s:
                                async with s.get(url, timeout=15) as resp:
                                    if resp.status == 200:
                                        content = await resp.read()
                                        os.makedirs(os.path.dirname(cover_path), exist_ok=True)
                                        with open(cover_path, "wb") as f:
                                            f.write(content)
                                        await self.set_channel_photo_from_path(peer, cover_path)
                        except Exception as e:
                            logging.warning("autochannel_loop: couldn't download/set cover: %s", e)
                    self._last_track_title = title
                    self._last_change_ts = now
                return
            if self._last_change_ts and (now - self._last_change_ts) > 600:
                await self.update_channel_title(peer, "⏸️Сейчас ничего не играет")
                await self.edit_or_send_one_message(peer, "-")
                await self.set_channel_photo_from_path(peer, self.config["IdleCoverPath"])
                self._last_track_title = None
                self._last_change_ts = now
        except Exception as e:
            logging.exception("Ошибка в autochannel_loop: %s", e)
