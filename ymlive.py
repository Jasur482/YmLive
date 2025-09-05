__version__ = (1, 2, 1)

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

# Устойчивое получение состояния плеера (без KeyError при непредвиденной структуре)
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
                logging.warning("get_current_track: redirect_ticket/host missing in first response: %s", data)
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
            ) as ws:
                await ws.send_str(json.dumps(to_send))
                recv = await asyncio.wait_for(ws.receive(), timeout=10)
                ynison = json.loads(recv.data)

                # Защита от отсутствия поля player_state
                player_state = ynison.get("player_state", {})
                player_queue = player_state.get("player_queue", {})
                track_index = player_queue.get("current_playable_index", -1)

                # если трека нет
                if track_index == -1:
                    paused_flag = player_state.get("status", {}).get("paused", True)
                    logging.info("get_current_track: no current playable (index=-1), paused=%s", paused_flag)
                    return {"success": False, "paused": paused_flag}

                playable_list = player_queue.get("playable_list", [])
                if not playable_list or track_index >= len(playable_list):
                    paused_flag = player_state.get("status", {}).get("paused", True)
                    logging.info("get_current_track: playable_list empty or index out of range, paused=%s", paused_flag)
                    return {"success": False, "paused": paused_flag}

                track_entry = playable_list[track_index]

            # Получаем полную информацию о треке через клиент
            playable_id = track_entry.get("playable_id")
            if not playable_id:
                logging.warning("get_current_track: playable_id missing in track_entry: %s", track_entry)
                return {"success": False}

            info = await client.tracks_download_info(playable_id, True)
            track = await client.tracks(playable_id)

            # track может быть списком; приводим к элементу
            track_item = track[0] if isinstance(track, (list, tuple)) and track else track

            res = {
                "paused": player_state.get("status", {}).get("paused", False),
                "duration_ms": player_state.get("status", {}).get("duration_ms", 0),
                "progress_ms": player_state.get("status", {}).get("progress_ms", 0),
                "entity_id": player_queue.get("entity_id", ""),
                "repeat_mode": player_queue.get("options", {}).get("repeat_mode"),
                "entity_type": player_queue.get("entity_type"),
                "track": track_item,
                "info": info,
                "success": True,
            }
            return res

    except Exception as e:
        logging.exception("Failed to get current track: %s", e)
        return {"success": False}


class YmLive(loader.Module):
    '''Модуль для демонстрации играющей песни в Яндекс.Музыке'''

    strings = {
        "name": "YandexMusicLive",
        "_text_token": "Токен аккаунта Яндекс Музыки",
        "_text_id": "ID канала, который будет использоваться для показа треков...",
        "_text_idle": "Путь к фото-заглушке, когда ничего не играет",
        "on/off": "YandexMusicLive теперь {}",
        "channel_id_error": "В конфиге не указан ID канала. Исправь это!",
    }

    def __init__(self):
        self.config = loader.ModuleConfig(
            loader.ConfigValue("YandexMusicToken", None, lambda: self.strings["_text_token"], validator=loader.validators.Hidden()),
            loader.ConfigValue("channel_id", None, lambda: self.strings["_text_id"], validator=loader.validators.TelegramID()),
            loader.ConfigValue("IdleCoverPath", "hikka_downloads/idle_cover.jpg", lambda: self.strings["_text_idle"]),
        )
        # Для отслеживания таймаута и последнего трека
        self._last_track_title = None
        self._last_change_ts = 0

    async def client_ready(self, client, db):
        self.client = client
        self.db = db

    async def add_bot_to_channel(self, channel_id):
        """Добавление бота в канал и выдача прав администратора (попытка)"""
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
            logging.warning("add_bot_to_channel: не удалось выдать права боту: %s", e)

    async def get_current_track(self, token):
        """Wrapper: возвращает либо dict с полями title/artists/cover/paused, либо None/short dict при паузе"""
        try:
            client = ClientAsync(token)
            await client.init()
            resp = await get_current_track(client, token)
            await client.stop()  # закрываем клиент
            if not resp or not resp.get("success"):
                # если пришёл только paused
                if resp and "paused" in resp:
                    return {"paused": resp.get("paused")}
                return None

            track = resp.get("track")
            if not track:
                return None

            # track — объект/словарь с нужными полями
            title = track.get("title") or ""
            artists = [a.get("name") for a in track.get("artists", [])] if track.get("artists") else []
            duration_ms = int(track.get("duration_ms", 0)) if track.get("duration_ms") else 0
            cover_uri = track.get("cover_uri") or track.get("cover") or track.get("albums", [{}])[0].get("cover_uri")

            return {
                "title": title,
                "artists": artists,
                "duration_ms": duration_ms,
                "cover_uri": cover_uri,
                "paused": resp.get("paused", False)
            }
        except Exception as e:
            logging.exception("Ошибка при get_current_track wrapper: %s", e)
            return None

    async def update_channel_title(self, channel_id, track_name):
        """Обновление названия канала (только если отличается)"""
        try:
            # ставим title (inline bot API требует -100{channel_id})
            await self.inline.bot.set_chat_title(int(f'-100{channel_id}'), track_name)
        except Exception as e:
            logging.warning("update_channel_title: не удалось изменить title: %s", e)

    async def edit_or_send_one_message(self, channel_id, text):
        """Редактируем единственное сообщение в канале или отправляем новое и сохраняем id в БД"""
        try:
            peer = channel_id  # в оригинальном коде так использовалось
            # пробуем взять сохраненный id
            saved_id = self.get("channel_msg_id")
            if saved_id:
                try:
                    await self._client.edit_message(peer, saved_id, text)
                    return
                except Exception:
                    # если не получилось — попытаемся редактировать последний
                    logging.info("edit_or_send_one_message: saved message id не доступно, попробую последние сообщения")
            msgs = await self._client.get_messages(peer, limit=1)
            if msgs and len(msgs) > 0:
                try:
                    await msgs[0].edit(text)
                    self.set("channel_msg_id", msgs[0].id)
                    return
                except Exception:
                    pass
            # если ничего не получилось — отправляем новое сообщение
            msg = await self._client.send_message(peer, text)
            self.set("channel_msg_id", msg.id)
        except Exception as e:
            logging.exception("edit_or_send_one_message: %s", e)

    async def set_channel_photo_from_path(self, channel_id, path):
        """Устанавливаем фото канала из локального файла, если файл существует"""
        if not path or not os.path.exists(path):
            logging.info("set_channel_photo_from_path: файл не найден: %s", path)
            return
        try:
            file = await self._client.upload_file(path)
            await self._client(EditPhotoRequest(channel=int(channel_id), photo=InputChatUploadedPhoto(file)))
        except Exception as e:
            logging.exception("set_channel_photo_from_path: %s", e)

    @loader.command(ru_doc="- включить/выключить YaLive")
    async def yalive(self, message):
        """Включение или выключение автоматического обновления названия канала"""
        if not self.config["channel_id"]:
            await utils.answer(message, self.strings["channel_id_error"])
            return

        if not self.get("ymlive_bot_added"):
            await self.add_bot_to_channel(self.config["channel_id"])

        autochannel_status = self.get("autochannel", False)
        self.set("autochannel", not autochannel_status)
        status_text = "enabled" if not autochannel_status else "disabled"
        await utils.answer(message, self.strings["on/off"].format(status_text))

    async def setidlepiccmd(self, message):
        """Сохранить фото-заглушку. Использование: .setidlepic (в ответ на фото)"""
        reply = await message.get_reply_message()
        if not reply or not reply.photo:
            await utils.answer(message, "<b>Нужно ответить на фото!</b>")
            return
        path = "hikka_downloads/idle_cover.jpg"
        await self.client.download_media(reply.photo, path)
        self.config["IdleCoverPath"] = path
        await utils.answer(message, f"<b>Idle обложка сохранена!</b> ({path})")

    @loader.loop(interval=30, autostart=True)
    async def autochannel_loop(self):
        """Цикл автообновления"""
        if not self.get("autochannel"):
            return
        if not self.config["channel_id"] or not self.config["YandexMusicToken"]:
            logging.debug("autochannel_loop: channel_id или токен не заданы")
            return

        try:
            track_info = await self.get_current_track(self.config["YandexMusicToken"])
            now = time.time()

            # Если API вернул dict с paused=True (возможно пусто или пауза) — сразу ставим паузу
            if isinstance(track_info, dict) and track_info.get("paused") is True:
                # поставить title паузы
                await self.update_channel_title(self.config["channel_id"], "⏸️Сейчас ничего не играет")
                # сообщение — дефис
                await self.edit_or_send_one_message(self.config["channel_id"], "-")
                # фото — Idle из конфига
                await self.set_channel_photo_from_path(self.config["channel_id"], self.config["IdleCoverPath"])
                # сброс состояния
                self._last_track_title = None
                self._last_change_ts = now
                return

            # Если получили трек
            if track_info:
                # нормальный трек
                artists = ", ".join(track_info.get("artists", [])) or "-"
                title = track_info.get("title", "") or "-"
                # если трек поменялся — обновляем всё
                if title != self._last_track_title:
                    # title (только имя трека в названии канала)
                    await self.update_channel_title(self.config["channel_id"], title)
                    # единое сообщение — авторы
                    await self.edit_or_send_one_message(self.config["channel_id"], artists)
                    # картинка — пытаемся скачать обложку и выставить
                    cover_uri = track_info.get("cover_uri")
                    if cover_uri:
                        # cover_uri может быть вида "some.path/%%/cover.jpg" — приводим к https
                        url = cover_uri
                        if url.startswith("//"):
                            url = "https:" + url
                        elif url.startswith("http"):
                            pass
                        else:
                            url = "https://" + url
                        # заменяем шаблон размера если есть
                        url = url.replace("%%", "400x400")
                        cover_path = "hikka_downloads/current_cover.jpg"
                        try:
                            async with aiohttp.ClientSession() as s:
                                async with s.get(url, timeout=15) as r:
                                    if r.status == 200:
                                        content = await r.read()
                                        os.makedirs(os.path.dirname(cover_path), exist_ok=True)
                                        with open(cover_path, "wb") as f:
                                            f.write(content)
                                        await self.set_channel_photo_from_path(self.config["channel_id"], cover_path)
                        except Exception as e:
                            logging.warning("Не удалось скачать/поставить cover: %s", e)

                    # сохраняем время изменения
                    self._last_track_title = title
                    self._last_change_ts = now
                return

            # Если track_info is None или неуспешный ответ — используем план Б (таймаут 10 минут)
            if self._last_change_ts and (now - self._last_change_ts) > 600:
                await self.update_channel_title(self.config["channel_id"], "⏸️Сейчас ничего не играет")
                await self.edit_or_send_one_message(self.config["channel_id"], "-")
                await self.set_channel_photo_from_path(self.config["channel_id"], self.config["IdleCoverPath"])
                self._last_track_title = None
                self._last_change_ts = now

        except Exception as e:
            logging.exception("Ошибка в autochannel_loop: %s", e)
