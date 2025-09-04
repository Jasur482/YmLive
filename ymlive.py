__version__ = (1, 1, 0)

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

# функция получения текущего трека (оставляем твою)
async def get_current_track(client, token):
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
                track_index = ynison["player_state"]["player_queue"]["current_playable_index"]
                if track_index == -1:
                    return {"success": False}

                track = ynison["player_state"]["player_queue"]["playable_list"][track_index]

            await session.close()
            track = await client.tracks(track["playable_id"])
            return {
                "paused": ynison["player_state"]["status"]["paused"],
                "track": track,
                "success": True,
            }

    except Exception as e:
        logging.error(f"Failed to get current track: {e}")
        return {"success": False}


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
        try:
            client = ClientAsync(token)
            await client.init()
            return await get_current_track(client, token)
        except Exception as e:
            logging.error(f"Ошибка при получении трека: {e}")
            return None

    async def update_channel(self, channel_id, title, author, cover_path=None):
        """Обновление канала (title + сообщение + аватарка)"""
        try:
            # обновляем title
            await self.client(EditAdminRequest(
                channel=int(channel_id),
                user_id=self.inline.bot_username,
                admin_rights=ChatAdminRights(change_info=True),
                rank="YandexMusicLiveBot"
            ))
            await self.inline.bot.set_chat_title(int(f"-100{channel_id}"), title)

            # обновляем сообщение
            msgs = await self.client.get_messages(channel_id, limit=1)
            if msgs:
                await msgs[0].edit(author)
            else:
                await self.client.send_message(channel_id, author)

            # обновляем фото
            if cover_path and os.path.exists(cover_path):
                file = await self.client.upload_file(cover_path)
                await self.client(EditPhotoRequest(
                    channel=int(channel_id),
                    photo=InputChatUploadedPhoto(file)
                ))

        except Exception as e:
            logging.error(f"Ошибка при обновлении канала: {e}")

    @loader.loop(interval=30, autostart=True)
    async def autochannel_loop(self):
        if not self.config["channel_id"] or not self.config["YandexMusicToken"]:
            return
        try:
            track_info = await self.get_current_track(self.config["YandexMusicToken"])
            if track_info and track_info.get("success") and not track_info["paused"]:
                track = track_info["track"][0]
                title = track["title"]
                artists = ", ".join([a["name"] for a in track["artists"]])
                cover_uri = track.get("cover_uri")
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
                if self.idle_counter >= 20:  # 20 * 30сек = 10 мин
                    await self.update_channel(
                        self.config["channel_id"],
                        "⏸️Сейчас ничего не играет",
                        "-",
                        self.config["IdleCoverPath"]
                    )
        except Exception as e:
            logging.error(f"Ошибка в autochannel_loop: {e}")
