@loader.loop(interval=30, autostart=True)
    async def autochannel_loop(self):
        """Цикл для автоматического обновления названия канала и сообщений каждые 30 секунд"""
        if not self.get("autochannel"):
            return
        if not self.config["channel_id"]:
            await self.inline.bot.send_message(self.client._tg_id, self.strings["_from_bot_channel_error"])
            self.set("autochannel", False)
            return

        token = self.config
        channel_id = self.config["channel_id"]
        
        # Загружаем идентификаторы сообщений из БД
        self._history_msg_id = self.db.get("YandexMusicLive", "history_msg_id", None)
        self._artists_msg_id = self.db.get("YandexMusicLive", "artists_msg_id", None)
        self._last_track_id = self.db.get("YandexMusicLive", "last_track_id", None)

        try:
            client = ClientAsync(token)
            await client.init()
            respond = await get_current_track(client, token)
            now = time.time()

            # Если неуспех или пауза — обновляем сообщения
            if not respond.get("success") or respond.get("paused") is True:
                await self.update_channel_title(channel_id, "⏸️ Сейчас ничего не играет")
                
                # Обновляем сообщение с артистами
                artists_text = "—"
                await self.update_and_persist_message(channel_id, artists_text, "artists")
                
                # Сбрасываем ID последнего трека для следующего цикла
                if self._last_track_id:
                    self.db.set("YandexMusicLive", "last_track_id", None)
                    self._last_track_id = None
                
                self._last_track_title = None
                self._last_change_ts = now
                return

            track = respond.get("track")
            
            # Проверяем, изменился ли трек
            if track["id"] == self._last_track_id:
                # Трек не изменился, просто обновляем время
                self._last_change_ts = now
                return

            # Трек изменился, выполняем полный цикл обновления
            self._last_track_id = track["id"]
            self.db.set("YandexMusicLive", "last_track_id", self._last_track_id)
            
            # 1. Обновляем название канала (только название трека)
            new_title = track["title"]
            await self.update_channel_title(channel_id, new_title)

            # 2. Обновляем сообщение с историей треков
            history_text = await self.get_recent_tracks_history(client)
            await self.update_and_persist_message(channel_id, history_text, "history")
            
            # 3. Обновляем сообщение с артистами
            artists = ", ".join([artist["name"] for artist in track["artists"]])
            await self.update_and_persist_message(channel_id, artists, "artists")

            self._last_change_ts = now
            self._last_track_title = new_title
        
        except Exception as e:
            logging.error(f"Ошибка в autochannel_loop: {e}")


    async def get_recent_tracks_history(self, client):
        """
        Получение истории прослушиваний и форматирование в HTML.
        """
        try:
            feed = await client.feed()
            recent_tracks = feed.recent_tracks
            if not recent_tracks or not recent_tracks.tracks:
                return "История треков: (Нет данных)"

            history_list =
            seen_ids = set()
            
            for item in recent_tracks.tracks:
                if len(history_list) >= 10:
                    break
                
                if item.id in seen_ids:
                    continue
                seen_ids.add(item.id)

                track_info = await client.tracks(item.id)
                track_obj = track_info
                
                if not track_obj.albums:
                    continue
                
                album_id = track_obj.albums.id
                track_id = track_obj.id
                track_title = track_obj.title
                artists = ", ".join([a.name for a in track_obj.artists])
                
                track_url = f"https://music.yandex.ru/album/{album_id}/track/{track_id}"
                
                history_list.append(f"<a href='{track_url}'>{track_title} - {utils.escape_html(artists)}</a>")

            if not history_list:
                return "История треков: (Нет данных)"
                
            history_text = "<b>История треков:</b>\n" + "\n".join(history_list)
            return history_text
        except Exception as e:
            logging.error(f"Ошибка при получении истории треков: {e}")
            return "История треков: (Ошибка загрузки)"

    async def update_and_persist_message(self, channel_id, text, msg_type):
        """
        Вспомогательная функция для обновления или отправки нового сообщения
        и сохранения его ID в БД.
        """
        msg_id_key = f"{msg_type}_msg_id"
        msg_id = self.db.get("YandexMusicLive", msg_id_key, None)
        
        try:
            if msg_id:
                # Попытка отредактировать существующее сообщение
                await self.inline.bot.edit_message(channel_id, msg_id, text, parse_mode="HTML")
            else:
                # Если ID нет, отправляем новое сообщение
                new_msg = await self.inline.bot.send_message(channel_id, text, parse_mode="HTML")
                self.db.set("YandexMusicLive", msg_id_key, new_msg.id)
                logging.info(f"Отправлено новое сообщение типа '{msg_type}' с ID {new_msg.id}")
        except Exception as e:
            logging.error(f"Не удалось обновить/отправить сообщение '{msg_type}': {e}")
            # Если произошла ошибка, сбрасываем ID, чтобы при следующем цикле отправить новое
            self.db.set("YandexMusicLive", msg_id_key, None)
            
    async def update_channel_title(self, channel_id, track_name):
        """Обновление названия канала"""
        try:
            channel_info = await self.client.get_fullchannel(channel_id)
            current_title = channel_info.chats.title
            if current_title!= track_name:
                await self.inline.bot.set_chat_title(int(f'-100{channel_id}'), track_name)
                messages = await self.client.get_messages(channel_id, limit=1)
                if messages and messages.action:
                    await messages.delete()
        except Exception as e:
            logging.error(f"Ошибка при изменении названия канала: {e}")

# Весь новый код, включая изменения в импортах и методах
__version__ = (1, 1, 0)

import json
import random
import string
import logging
import asyncio
import aiohttp
import time  # ADDED
from.. import loader, utils
from yandex_music import ClientAsync
from telethon.tl.types import ChatAdminRights
from telethon.tl.functions.channels import EditAdminRequest
from telethon.errors import MessageDeleteForbiddenError, MessageNotModifiedError # ADDED

# https://github.com/FozerG/YandexMusicRPC/blob/main/main.py#L133
async def get_current_track(client, token):
    device_info = {"app_name": "Chrome","type": 1,}

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
                print(f"Invalid response structure: {data}")
                return {"success": False}

            new_ws_proto = ws_proto.copy()
            new_ws_proto = data["redirect_ticket"]

            to_send = {
                "update_full_state": {
                    "player_state": {
                        "player_queue": {
                            "current_playable_index": -1,
                            "entity_id": "",
                            "entity_type": "VARIOUS",
                            "playable_list":,
                            "options": {"repeat_mode": "NONE"},
                            "entity_context": "BASED_ON_ENTITY_BY_DEFAULT",
                            "version": {
                                "device_id": ws_proto,
                                "version": 9021243204784341000,
                                "timestamp_ms": 0,
                            },
                            "from_optional": "",
                        },
                        "status": {
                            "duration_ms": 0,
                            "paused": True,
                            "playback_speed": 1,
                            "progress_ms": 0,
                            "version": {
                                "device_id": ws_proto,
                                "version": 8321822175199937000,
                                "timestamp_ms": 0,
                            },
                        },
                    },
                    "device": {
                        "capabilities": {
                            "can_be_player": True,
                            "can_be_remote_controller": False,
                            "volume_granularity": 16,
                        },
                        "info": {
                            "device_id": ws_proto,
                            "type": "WEB",
                            "title": "Chrome Browser",
                            "app_name": "Chrome",
                        },
                        "volume_info": {"volume": 0},
                        "is_shadow": True,
                    },
                    "is_currently_active": False,
                },
                "rid": "ac281c26-a047-4419-ad00-e4fbfda1cba3",
                "player_action_timestamp_ms": 0,
                "activity_interception_type": "DO_NOT_INTERCEPT_BY_DEFAULT",
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
                track_index = ynison["player_state"]["player_queue"]["current_playable_index"]
                if track_index == -1:
                    print("No track is currently playing.")
                    return {"success": False, "paused": ynison["player_state"]["status"]["paused"]}

                track = ynison["player_state"]["player_queue"]["playable_list"][track_index]

            await session.close()
            info = await client.tracks_download_info(track["playable_id"], True)
            track_obj = await client.tracks(track["playable_id"]) # ADDED
            res = {
                "paused": ynison["player_state"]["status"]["paused"],
                "duration_ms": ynison["player_state"]["status"]["duration_ms"],
                "progress_ms": ynison["player_state"]["status"]["progress_ms"],
                "entity_id": ynison["player_state"]["player_queue"]["entity_id"],
                "repeat_mode": ynison["player_state"]["player_queue"]["options"]["repeat_mode"],
                "entity_type": ynison["player_state"]["player_queue"]["entity_type"],
                "track": track_obj,  # CHANGED: Возвращаем полный объект трека
                "info": info,
                "success": True,
            }
            return res

    except Exception as e:
        print(f"Failed to get current track: {str(e)}")
        return {"success": False}


class YmLive(loader.Module):
    '''Модуль для демонстрации играющей песни в Яндекс.Музыке'''

    strings = {
        "name": "YandexMusicLive",
        
        "_text_token": "Токен аккаунта Яндекс Музыки",
        "_text_id": "ID канала, который будет использоваться для показа треков...",

        "on/off": "YandexMusicLive теперь {}",
        'channel_id_error': "В конфиге не указан ID канала. Исправь это!",

        "_from_bot_channel_error": (
            "Не найден ID канала в конфиге. Пожалуйста исправь это для "
            "дальнейшего использования модуля..."
        ),
        'token_from_YmNow': (
            "У вас установлен модуль YmNow и в его конфиге я нашел токен. "
            "Для вашего удобства токен автоматически выставлен в конфиг. "
            "Приятного использования :)"
        ),
        "tutor": (
            "🎉 Добро пожаловать в модуль YandexMusicLive!\n"
            "Вы успешно загрузили модуль, который позволяет отображать играющую музыку "
            "из Яндекс.Музыки прямо в названии вашего канала!\n\n"
            "🌟 Чтобы модуль начал работать, выполните следующие шаги:\n"
            "1) <b>Создайте канал:</b> Cоздайте новый канал, в котором будет "
            "отображаться играющий сейчас трек, и закрепите этот канал в своем профиле.\n\n"
            "2) <b>Настройка токена Яндекс.Музыки:</b> Перейдите в <code>{}config YandexMusicLive</code>"
            " -> YandexMusicToken и вставьте ваш токен Яндекс.Музыки. <a href='{}'>(Туториал на получение токена)</a>\n\n"
            "3) <b>Настройка ID канала:</b> Перейдите в <code>{}config YandexMusicLive</code> -> channel_id"
            " и вставьте ID вашего канала. \n"
            "  Если вы не знаете, как получить ID канала - Напишите в канал"
            " сообщение с текстом <code>{}e m.chat.id</code> и вставьте в конфиг то, что вам выдаст ЮзерБот\n\n"
            "4) <b>Переустановите модуль</b> После выполнения всех настроек переустановите модуль, чтобы завершить процесс настройки."
        )
    }

    def __init__(self):
        self.config = loader.ModuleConfig(
            loader.ConfigValue(
                "YandexMusicToken", 
                None, 
                lambda: self.strings["_text_token"], 
                validator=loader.validators.Hidden()
            ),
            loader.ConfigValue(
                "channel_id",
                None,
                lambda: self.strings["_text_id"],
                validator=loader.validators.TelegramID()
            ),
        )
        self._last_track_title = None
        self._last_change_ts = 0
        self._history_msg_id = None # ADDED
        self._artists_msg_id = None # ADDED
        self._last_track_id = None # ADDED

    async def client_ready(self, client, db):
        """Инициализация клиента и базы данных"""
        self.client = client
        self.db = db

    async def on_dlmod(self):
        """Действия при загрузке модуля"""
        if self.get("new_")!= False:
            await self.inline.bot.send_message(
                self.client._tg_id, 
                self.strings("tutor").format(
                    self.get_prefix(), 
                    "https://github.com/MarshalX/yandex-music-api/discussions/513#discussioncomment-2729781",
                    self.get_prefix(),
                    self.get_prefix()
                )
            )
            self.set("new_", False)

        if self.config and self.config.startswith("y0_"):
            await self.add_bot_to_channel(self.config["channel_id"])

    async def add_bot_to_channel(self, channel_id):
        """Добавление бота в канал и выдача прав администратора"""
        try:
            await self.client(
                EditAdminRequest(
                    channel=int(channel_id),
                    user_id=self.inline.bot_username,
                    admin_rights=ChatAdminRights(change_info=True, delete_messages=True), # ADDED
                    rank="YandexMusicLiveBot"
                )
            )
            self.set("ymlive_bot_added", True)
        except Exception as e:
            logging.error(f"Не удалось выдать боту права в канале: {e}")

    # Устаревшая функция, больше не используется
    # async def get_current_track(self, token):
    #   ...

    async def update_channel_title(self, channel_id, track_name):
        """Обновление названия канала, если оно отличается от текущего трека"""
        try:
            channel_info = await self.client.get_fullchannel(channel_id)
            current_title = channel_info.chats.title
            if current_title!= track_name:
                await self.inline.bot.set_chat_title(int(f'-100{channel_id}'), track_name)
                # Удаляем служебное сообщение о смене названия
                messages = await self.client.get_messages(channel_id, limit=1)
                if messages and messages.action:
                    await messages.delete()
        except Exception as e:
            logging.error(f"Ошибка при изменении названия канала: {e}")

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
        
    async def get_recent_tracks_history(self, client):
        """
        Получение истории прослушиваний и форматирование в HTML.
        """
        try:
            feed = await client.feed()
            recent_tracks = feed.recent_tracks
            if not recent_tracks or not recent_tracks.tracks:
                return "<b>История треков:</b> (Нет данных)"

            history_list =
            seen_ids = set()
            
            for item in recent_tracks.tracks:
                if len(history_list) >= 10:
                    break
                
                if not item.id or item.id in seen_ids:
                    continue
                seen_ids.add(item.id)

                track_info = await client.tracks(item.id)
                track_obj = track_info
                
                if not track_obj.albums:
                    continue
                
                album_id = track_obj.albums.id
                track_id = track_obj.id
                track_title = track_obj.title
                artists = ", ".join([a.name for a in track_obj.artists])
                
                track_url = f"https://music.yandex.ru/album/{album_id}/track/{track_id}"
                
                history_list.append(f"<a href='{track_url}'>{track_title} - {utils.escape_html(artists)}</a>")

            if not history_list:
                return "<b>История треков:</b> (Нет данных)"
                
            history_text = "<b>История треков:</b>\n" + "\n".join(history_list)
            return history_text
        except Exception as e:
            logging.error(f"Ошибка при получении истории треков: {e}")
            return "<b>История треков:</b> (Ошибка загрузки)"

    async def update_and_persist_message(self, channel_id, text, msg_type):
        """
        Вспомогательная функция для обновления или отправки нового сообщения
        и сохранения его ID в БД.
        """
        msg_id_key = f"{msg_type}_msg_id"
        msg_id = self.db.get("YandexMusicLive", msg_id_key, None)
        
        try:
            if msg_id:
                # Попытка отредактировать существующее сообщение
                await self.inline.bot.edit_message(channel_id, msg_id, text, parse_mode="HTML")
            else:
                # Если ID нет, отправляем новое сообщение
                new_msg = await self.inline.bot.send_message(channel_id, text, parse_mode="HTML")
                self.db.set("YandexMusicLive", msg_id_key, new_msg.id)
                logging.info(f"Отправлено новое сообщение типа '{msg_type}' с ID {new_msg.id}")
        except MessageNotModifiedError:
            pass  # Игнорируем ошибку, если текст не изменился
        except Exception as e:
            logging.error(f"Не удалось обновить/отправить сообщение '{msg_type}': {e}")
            # Если произошла ошибка, сбрасываем ID, чтобы при следующем цикле отправить новое
            self.db.set("YandexMusicLive", msg_id_key, None)

    @loader.loop(interval=30, autostart=True)
    async def autochannel_loop(self):
        """Цикл для автоматического обновления названия канала и сообщений каждые 30 секунд"""
        if not self.get("autochannel"):
            return
        if not self.config["channel_id"]:
            await self.inline.bot.send_message(self.client._tg_id, self.strings["_from_bot_channel_error"])
            self.set("autochannel", False)
            return

        token = self.config
        channel_id = self.config["channel_id"]
        
        # Загружаем идентификаторы сообщений из БД
        self._history_msg_id = self.db.get("YandexMusicLive", "history_msg_id", None)
        self._artists_msg_id = self.db.get("YandexMusicLive", "artists_msg_id", None)
        self._last_track_id = self.db.get("YandexMusicLive", "last_track_id", None)

        try:
            client = ClientAsync(token)
            await client.init()
            respond = await get_current_track(client, token)
            now = time.time()

            # Если неуспех или пауза — обновляем сообщения
            if not respond.get("success"):
                await self.update_channel_title(channel_id, "⏸️ Сейчас ничего не играет")
                
                artists_text = "—"
                await self.update_and_persist_message(channel_id, artists_text, "artists")
                
                # Сбрасываем ID последнего трека
                if self._last_track_id:
                    self.db.set("YandexMusicLive", "last_track_id", None)
                    self._last_track_id = None
                
                self._last_track_title = None
                self._last_change_ts = now
                return

            track = respond.get("track")
            paused = respond.get("paused")
            
            # Если пауза, но ответ success
            if paused:
                await self.update_channel_title(channel_id, "⏸️ Сейчас ничего не играет")
                artists_text = "—"
                await self.update_and_persist_message(channel_id, artists_text, "artists")
                
                if self._last_track_id:
                    self.db.set("YandexMusicLive", "last_track_id", None)
                    self._last_track_id = None
                
                self._last_track_title = None
                self._last_change_ts = now
                return
            
            # Проверяем, изменился ли трек, используя ID
            if track.id == self._last_track_id:
                # Трек не изменился, просто обновляем время
                self._last_change_ts = now
                return

            # Трек изменился, выполняем полный цикл обновления
            self._last_track_id = track.id
            self.db.set("YandexMusicLive", "last_track_id", self._last_track_id)
            
            # 1. Обновляем название канала (только название трека)
            new_title = track.title
            await self.update_channel_title(channel_id, new_title)

            # 2. Обновляем сообщение с историей треков
            history_text = await self.get_recent_tracks_history(client)
            await self.update_and_persist_message(channel_id, history_text, "history")
            
            # 3. Обновляем сообщение с артистами
            artists = ", ".join([artist.name for artist in track.artists])
            await self.update_and_persist_message(channel_id, artists, "artists")

            self._last_change_ts = now
            self._last_track_title = new_title
        
        except Exception as e:
            logging.error(f"Ошибка в autochannel_loop: {e}")
