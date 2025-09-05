__version__ = (1, 2, 0)

import json
import random
import string
import logging
import asyncio
import aiohttp
import os
from .. import loader, utils
from yandex_music import ClientAsync
from telethon.tl.types import ChatAdminRights, InputChatUploadedPhoto
from telethon.tl.functions.channels import EditAdminRequest, EditPhotoRequest


# --- утилита для работы с WS ---
async def _raw_get_current_track_ws(token: str):
    device_info = {"app_name": "Chrome", "type": 1}
    ws_proto = {
        "Ynison-Device-Id": "".join([random.choice(string.ascii_lowercase) for _ in range(16)]),
        "Ynison-Device-Info": json.dumps(device_info),
    }
    timeout = aiohttp.ClientTimeout(total=15, connect=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
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
                data = json.loads(recv.data)

            if "redirect_ticket" not in data or "host" not in data:
                return {"success": False}

            new_ws_proto = ws_proto.copy()
            new_ws_proto["Ynison-Redirect-Ticket"] = data["redirect_ticket"]
            to_send = {"update_full_state": {"player_state": {"player_queue": {"current_playable_index": -1}}}}

            async with session.ws_connect(
                url=f"wss://{data['host']}/ynison_state.YnisonStateService/PutYnisonState",
                headers={
                    "Sec-WebSocket-Protocol": f"Bearer, v2, {json.dumps(new_ws_proto)}",
                    "Origin": "http://music.yandex.ru",
                    "Authorization": f"OAuth {token}",
                },
                timeout=10,
                method="GET",
            ) as ws:
                await ws.send_str(json.dumps(to_send))
                recv = await asyncio.wait_for(ws.receive(), timeout=10)
                ynison = json.loads(recv.data)
                return ynison
    except Exception as e:
        logging.error(f"_raw_get_current_track_ws failed: {e}")
        return None


class YmLive(loader.Module):
    """🎵 YandexMusicLive — трек в названии канала"""

    strings = {
        "name": "YandexMusicLive",
        "_text_token": "Токен Яндекс.Музыки",
        "_text_id": "ID канала",
        "_text_idle": "Путь к фото, когда ничего не играет",
        "channel_id_error": "В конфиге не указан ID канала.",
    }

    def __init__(self):
        self.config = loader.ModuleConfig(
            loader.ConfigValue(
                "YandexMusicToken", None,
                lambda: self.strings["_text_token"], validator=loader.validators.Hidden()
            ),
            loader.ConfigValue(
                "channel_id", None,
                lambda: self.strings["_text_id"], validator=loader.validators.TelegramID()
            ),
            loader.ConfigValue(
                "IdleCoverPath", "hikka_downloads/idle_cover.jpg",
                lambda: self.strings["_text_idle"]
            ),
        )
        self.last_track = None
        self.idle_counter = 0

    async def client_ready(self, client, db):
        self.client = client
        self.db = db

    async def setidlepiccmd(self, message):
        """Сохранить фото-заглушку. Использование: .setidlepic <reply to photo>"""
        reply = await message.get_reply_message()
        if not reply or not reply.photo:
            await utils.answer(message, "<b>Нужно ответить на фото!</b>")
            return

        path = "hikka_downloads/idle_cover.jpg"
        await self.client.download_media(reply.photo, path)
        self.config["IdleCoverPath"] = path
        await utils.answer(message, f"<b>Idle обложка сохранена!</b> ({path})")

    async def get_current_track(self, token):
        """Получить текущий трек из Яндекс.Музыки"""
        try:
            client = ClientAsync(token)
            await client.init()
            raw = await _raw_get_current_track_ws(token)
            if not raw:
                await client.close()
                return None

            queue = raw.get("player_state", {}).get("player_queue", {})
            idx = queue.get("current_playable_index", -1)
            if idx == -1:
                paused_flag = raw.get("player_state", {}).get("status", {}).get("paused", True)
                await client.close()
                return {"paused": paused_flag}

            plist = queue.get("playable_list", [])
            if not plist or idx >= len(plist):
                await client.close()
                return None

            playable_id = plist[idx].get("playable_id")
            track = await client.tracks(playable_id)
            await client.close()

            if isinstance(track, (list, tuple)) and track:
                track_item = track[0]
            else:
                track_item = track

            if not isinstance(track_item, dict):
                return None

            title = track_item.get("title") or ""
            artists = [a.get("name") for a in track_item.get("artists", [])] if track_item.get("artists") else []
            cover_uri = track_item.get("cover_uri") or track_item.get("cover") or None
            paused_flag = raw.get("player_state", {}).get("status", {}).get("paused", False)

            return {"title": title, "artists": artists, "cover_uri": cover_uri, "paused": paused_flag}

        except Exception as e:
            logging.exception("get_current_track exception: %s", e)
            return None

    async def update_channel(self, channel_id, title, author, cover_path=None):
        """Обновление канала: title + сообщение + обложка"""
        try:
            await self.inline.bot.set_chat_title(int(f"-100{channel_id}"), title)

            msgs = await self.client.get_messages(channel_id, limit=1)
            if msgs:
                await msgs[0].edit(author)
            else:
                await self.client.send_message(channel_id, author)

            if cover_path and os.path.exists(cover_path):
                file = await self.client.upload_file(cover_path)
                await self.client(EditPhotoRequest(
                    channel=int(channel_id),
                    photo=InputChatUploadedPhoto(file)
                ))

        except Exception as e:
            logging.error(f"Ошибка при обновлении канала: {e}")

    @loader.command(ru_doc="- включить/выключить YaLive")
    async def yalive(self, message):
        """Вкл/выкл автoобновление канала"""
        if not self.config["channel_id"]:
            await utils.answer(message, self.strings["channel_id_error"])
            return

        autochannel_status = self.get("autochannel", False)
        self.set("autochannel", not autochannel_status)
        status_text = "✅ включен" if not autochannel_status else "⛔ выключен"
        await utils.answer(message, f"<b>YandexMusicLive {status_text}</b>")

    @loader.loop(interval=30, autostart=True)
    async def autochannel_loop(self):
        if not self.get("autochannel"):
            return
        if not self.config["channel_id"] or not self.config["YandexMusicToken"]:
            return
        try:
            track_info = await self.get_current_track(self.config["YandexMusicToken"])
            if track_info and not track_info.get("paused", True):
                title = track_info["title"]
                artists = ", ".join(track_info["artists"]) if track_info["artists"] else "-"
                cover_uri = track_info.get("cover_uri")
                cover_path = None
                if cover_uri:
                    url = "https://" + cover_uri.replace("%%", "200x200")
                    cover_path = "hikka_downloads/current_cover.jpg"
                    async with aiohttp.ClientSession() as s:
                        async with s.get(url) as r:
                            with open(cover_path, "wb") as f:
                                f.write(await r.read())
                await self.update_channel(self.config["channel_id"], title, artists, cover_path)
                self.idle_counter = 0
            else:
                self.idle_counter += 1
                if self.idle_counter >= 20:  # 10 минут
                    await self.update_channel(
                        self.config["channel_id"],
                        "⏸️Сейчас ничего не играет",
                        "-",
                        self.config["IdleCoverPath"]
                    )
        except Exception as e:
            logging.error(f"Ошибка в autochannel_loop: {e}")
