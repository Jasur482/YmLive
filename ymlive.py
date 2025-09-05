__version__ = (1, 3, 0)

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

# --- НИЖЕ: низкоуровневое взаимодействие с Ynison WS (с защитой от неожиданных структур) ---
async def _raw_get_current_track_ws(token: str):
    """
    Возвращает:
      - None или {"success": False} при ошибках
      - {"success": False, "paused": True/False} если трека нет, но известно состояние paused
      - {"success": True, "player_state": {...}, "track_entry": {...}} если есть текущий трек
    """
    device_info = {"app_name": "Chrome", "type": 1}
    ws_proto = {
        "Ynison-Device-Id": "".join(random.choice(string.ascii_lowercase) for _ in range(16)),
        "Ynison-Device-Info": json.dumps(device_info),
    }

    timeout = aiohttp.ClientTimeout(total=15, connect=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Первый редирект
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
                    logging.warning("raw_ws: couldn't parse first response: %s", repr(recv.data))
                    return {"success": False}

            if "redirect_ticket" not in data or "host" not in data:
                logging.debug("raw_ws: redirect_ticket/host missing: %s", data)
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
                    logging.warning("raw_ws: couldn't parse second response: %s", repr(recv2.data))
                    return {"success": False}

                player_state = ynison.get("player_state", {})
                player_queue = player_state.get("player_queue", {})
                status = player_state.get("status", {})
                track_index = player_queue.get("current_playable_index", -1)

                if track_index == -1:
                    paused_flag = bool(status.get("paused", True))
                    logging.debug("raw_ws: no track currently, paused=%s", paused_flag)
                    return {"success": False, "paused": paused_flag}

                playable_list = player_queue.get("playable_list", [])
                if not playable_list or track_index >= len(playable_list):
                    paused_flag = bool(status.get("paused", True))
                    logging.debug("raw_ws: playable_list missing or index OOR, paused=%s", paused_flag)
                    return {"success": False, "paused": paused_flag}

                track_entry = playable_list[track_index]
                return {"success": True, "player_state": player_state, "track_entry": track_entry}
    except Exception as e:
        logging.exception("_raw_get_current_track_ws exception: %s", e)
        return {"success": False}


# --- Главный модуль ---
class YmLive(loader.Module):
    """YandexMusicLive — название канала = название трека, 1 сообщение = автор(ы), аватарка = обложка/idle"""

    strings = {
        "name": "YandexMusicLive",
        "_text_token": "Токен аккаунта Яндекс.Музыки",
        "_text_id": "ID канала (только цифры, без -100)",
        "_text_idle": "Путь к фото-заглушке (через .setidlepic)",
        "on/off": "YandexMusicLive теперь {}",
        "channel_id_error": "В конфиге не указан ID канала. Исправь это!",
        "setidle_no_reply": "Нужно ответить на фото командой .setidlepic",
        "setidle_ok": "Idle обложка сохранена: {}",
    }

    def __init__(self):
        # конфиг
        self.config = loader.ModuleConfig(
            loader.ConfigValue("YandexMusicToken", None, lambda: self.strings["_text_token"], validator=loader.validators.Hidden()),
            loader.ConfigValue("channel_id", None, lambda: self.strings["_text_id"], validator=loader.validators.TelegramID()),
            loader.ConfigValue("IdleCoverPath", "hikka_downloads/idle_cover.jpg", lambda: self.strings["_text_idle"]),
        )
        # state
        self._last_track_title = None
        self._last_change_ts = 0
        self._ym_client = None  # будет хранить ClientAsync (создаём лениво)

    async def client_ready(self, client, db):
        # Telethon client
        self._client = client
        self.db = db
        # inline bot доступен через self.inline.bot

    async def _normalize_channel_peer(self):
        # возвращаем int peer вида -100123456...
        raw = str(self.config["channel_id"] or "").strip()
        if not raw:
            return None
        if raw.startswith("-100"):
            try:
                return int(raw)
            except Exception:
                return None
        raw = raw.lstrip("+")
        try:
            return int(f"-100{raw}")
        except Exception:
            try:
                return int(raw)
            except Exception:
                return None

    async def _ensure_ym_client(self):
        """Создаёт ClientAsync один раз и переиспользует его (лениво)."""
        token = self.config["YandexMusicToken"]
        if not token:
            return None
        if self._ym_client:
            # если токен изменился — попытка создать новый клиент
            try:
                if getattr(self._ym_client, "_token", None) != token:
                    # не удаляем старый (библиотека не даёт общий метод корректного закрытия в текущей среде)
                    self._ym_client = ClientAsync(token)
                    await self._ym_client.init()
            except Exception:
                # просто попробуем создать новый клиент
                try:
                    self._ym_client = ClientAsync(token)
                    await self._ym_client.init()
                except Exception as e:
                    logging.exception("_ensure_ym_client create failed: %s", e)
                    self._ym_client = None
        else:
            try:
                self._ym_client = ClientAsync(token)
                await self._ym_client.init()
            except Exception as e:
                logging.exception("_ensure_ym_client init failed: %s", e)
                self._ym_client = None
        return self._ym_client

    async def get_current_track(self, token):
        """
        Возвращает:
          None                - ошибка / не удалось получить
          {"paused": True}    - известно, что на паузе
          {"title":..., "artists":[...], "cover_uri":..., "paused":False} - трек
        """
        try:
            # получаем raw от ws
            raw = await _raw_get_current_track_ws(token)
            if not raw or not raw.get("success"):
                # возможно вернулся paused
                if isinstance(raw, dict) and "paused" in raw:
                    return {"paused": bool(raw.get("paused"))}
                return None

            track_entry = raw.get("track_entry")
            if not track_entry:
                return None

            playable_id = track_entry.get("playable_id")
            if not playable_id:
                return None

            # создаём/переиспользуем клиент yandex_music
            client = await self._ensure_ym_client()
            if not client:
                # если не удалось создать клиент — возвращаем только paused если есть
                return None

            # Получаем трек (tracks возвращает объект/список)
            try:
                track = await client.tracks(playable_id)
            except Exception as e:
                logging.exception("client.tracks failed: %s", e)
                return None

            # tracks() может вернуть список
            if isinstance(track, (list, tuple)) and track:
                track_item = track[0]
            else:
                track_item = track

            if not isinstance(track_item, dict):
                logging.warning("get_current_track: unexpected track_item type: %s", type(track_item))
                return None

            title = track_item.get("title") or ""
            artists = [a.get("name") for a in track_item.get("artists", [])] if track_item.get("artists") else []
            cover_uri = track_item.get("cover_uri") or track_item.get("cover") or None
            paused_flag = bool(raw.get("player_state", {}).get("status", {}).get("paused", False))

            return {"title": title, "artists": artists, "cover_uri": cover_uri, "paused": paused_flag}

        except Exception as e:
            logging.exception("get_current_track exception: %s", e)
            return None

    async def add_bot_to_channel(self, channel_peer):
        """Пытаемся дать боту права change_info (не критично если упадёт)."""
        try:
            await self._client(
                EditAdminRequest(
                    channel=int(channel_peer),
                    user_id=self.inline.bot_username,
                    admin_rights=ChatAdminRights(change_info=True),
                    rank="YandexMusicLiveBot"
                )
            )
            self.set("ymlive_bot_added", True)
        except Exception as e:
            logging.info("add_bot_to_channel: %s", e)

    async def update_channel_title(self, peer, title):
        """Меняем title канала через inline.bot (отображается как title в профиле)."""
        try:
            await self.inline.bot.set_chat_title(int(peer), title)
        except Exception as e:
            logging.warning("update_channel_title failed: %s", e)

    async def edit_or_send_one_message(self, peer, text):
        """
        Редактируем одно сообщение в канале (если есть сохранённый id) или последний пост,
        иначе отправляем новое и сохраняем его id.
        """
        try:
            saved_id = self.get("channel_msg_id")
            if saved_id:
                try:
                    await self._client.edit_message(peer, saved_id, text)
                    return
                except Exception:
                    logging.info("edit_or_send_one_message: saved id not editable, will try last message")

            # пробуем редактировать последний
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

            # отправляем новое
            msg = await self._client.send_message(peer, text)
            self.set("channel_msg_id", msg.id)
        except Exception as e:
            logging.exception("edit_or_send_one_message: %s", e)

    async def set_channel_photo_from_path(self, peer, path):
        """Устанавливаем фото канала, если файл существует."""
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
        """Вкл/выкл автообновление имен/сообщения/аватарки"""
        if not self.config["channel_id"]:
            await utils.answer(message, self.strings["channel_id_error"])
            return

        peer = await self._normalize_channel_peer()
        if peer:
            # пробуем дать боту права, но не падаем если не получится
            await self.add_bot_to_channel(peer)

        autochannel_status = self.get("autochannel", False)
        self.set("autochannel", not autochannel_status)
        status_text = "enabled" if not autochannel_status else "disabled"
        await utils.answer(message, self.strings["on/off"].format(status_text))

    @loader.command(ru_doc="Сохранить фото-заглушку. Использование: .setidlepic (ответ на фото)")
    async def setidlepiccmd(self, message):
        """Сохранить фото-заглушку (ответ на фото)"""
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
        """Основной цикл — updates every 30s"""
        if not self.get("autochannel"):
            return

        channel_cfg = self.config["channel_id"]
        token = self.config["YandexMusicToken"]
        if not channel_cfg or not token:
            logging.debug("autochannel_loop: channel_id or token missing")
            return

        peer = await self._normalize_channel_peer()
        if not peer:
            logging.warning("autochannel_loop: cannot normalize channel id")
            return

        try:
            track_info = await self.get_current_track(token)
            now = time.time()

            # если API однозначно сказал paused -> ставим паузу
            if isinstance(track_info, dict) and track_info.get("paused") is True:
                await self.update_channel_title(peer, "⏸️Сейчас ничего не играет")
                await self.edit_or_send_one_message(peer, "-")
                await self.set_channel_photo_from_path(peer, self.config["IdleCoverPath"])
                self._last_track_title = None
                self._last_change_ts = now
                return

            # если пришёл трек
            if isinstance(track_info, dict) and track_info.get("title"):
                title = track_info.get("title") or "-"
                artists = ", ".join(track_info.get("artists", [])) or "-"
                cover_uri = track_info.get("cover_uri")

                # обновляем только если сменилось название трека
                if title != self._last_track_title:
                    # title канала — только имя трека
                    await self.update_channel_title(peer, title)

                    # единое сообщение — автор(ы)
                    await self.edit_or_send_one_message(peer, artists)

                    # обложка: пытаемся скачать и поставить
                    if cover_uri:
                        url = cover_uri
                        if url.startswith("//"):
                            url = "https:" + url
                        elif not url.startswith("http"):
                            url = "https://" + url
                        url = url.replace("%%", "400x400")
                        cover_path = "hikka_downloads/current_cover.jpg"
                        try:
                            os.makedirs(os.path.dirname(cover_path), exist_ok=True)
                            async with aiohttp.ClientSession() as s:
                                async with s.get(url, timeout=15) as resp:
                                    if resp.status == 200:
                                        content = await resp.read()
                                        with open(cover_path, "wb") as f:
                                            f.write(content)
                                        await self.set_channel_photo_from_path(peer, cover_path)
                        except Exception as e:
                            logging.warning("autochannel_loop: couldn't download/set cover: %s", e)

                    self._last_track_title = title
                    self._last_change_ts = now
                return

            # если ничего не пришло — проверяем таймаут 10 минут (план Б)
            if self._last_change_ts and (now - self._last_change_ts) > 600:
                await self.update_channel_title(peer, "⏸️Сейчас ничего не играет")
                await self.edit_or_send_one_message(peer, "-")
                await self.set_channel_photo_from_path(peer, self.config["IdleCoverPath"])
                self._last_track_title = None
                self._last_change_ts = now

        except Exception as e:
            logging.exception("Ошибка в autochannel_loop: %s", e)
