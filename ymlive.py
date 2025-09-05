# requires: yandex-music
import logging
import time
from yandex_music import ClientAsync

from .. import loader, utils

# credits: @hikariatama, @vsecoder, @usernein

logging.basicConfig(level=logging.INFO)

@loader.tds
class YandexMusicLiveMod(loader.Module):
    """Модуль для автоматического обновления названия канала в зависимости от текущего трека в Яндекс.Музыке"""

    strings = {
        "name": "YandexMusicLive",
        "channel_id_error": "🚫 **ID канала не указан.**\nУкажите его в конфиге модуля.",
        "on/off": "🎧 **Автоматическое обновление информации в канале {}!**",
        "_from_bot_channel_error": "🚫 **ID канала не указан в конфиге YandexMusicLive.**",
        "history_title": "<b>📜 История треков:</b>\n\n",
        "artist_placeholder": "-",
        "paused_title": "⏸️ Сейчас ничего не играет"
    }

    def __init__(self):
        self.config = loader.ModuleConfig(
            "YandexMusicToken", "", lambda: "Токен Яндекс.Музыки",
            "channel_id", "", lambda: "ID канала для изменения названия (без -100)",
        )
        self._last_track_title = None
        self._last_change_ts = None
        self._client = None
        self.ym_client = None

    async def client_ready(self, client, db):
        self._db = db
        self._client = client
        if self.config["YandexMusicToken"]:
            self.ym_client = ClientAsync(self.config["YandexMusicToken"])
            await self.ym_client.init()

    async def get_current_track(self):
        """Получение информации о текущем треке"""
        try:
            queues = await self.ym_client.queues_list()
            if not queues:
                return None

            last_queue = await self.ym_client.queue(queues[0].id)
            if last_queue.player_state.paused:
                return {"paused": True}

            track_id = last_queue.get_current_track()
            if not track_id:
                return None

            track = await track_id.fetch_track_async()
            artists = ", ".join(artist.name for artist in track.artists)
            return {
                "title": track.title,
                "artists": artists,
                "id": track.id,
                "album_id": track.albums[0].id if track.albums else '0',
                "paused": False,
            }
        except Exception as e:
            logging.error(f"Ошибка при получении трека из Яндекс.Музыки: {e}")
            return None

    async def update_channel_title(self, channel_id, title):
        """Обновление только названия канала и удаление сервисного сообщения"""
        try:
            channel_info = await self.inline.bot.get_chat(int(f'-100{channel_id}'))
            current_title = channel_info.title

            if current_title != title:
                await self.inline.bot.set_chat_title(int(f'-100{channel_id}'), title)
                messages = await self._client.get_messages(int(f'-100{channel_id}'), limit=1)
                if messages and messages[0].action:
                    await messages[0].delete()
        except Exception as e:
            logging.error(f"Ошибка при изменении названия канала: {e}")

    async def _edit_message(self, channel_id, message_id, text):
        """Безопасное редактирование сообщения"""
        try:
            await self.inline.bot.edit_message_text(
                chat_id=int(f'-100{channel_id}'),
                message_id=message_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        except Exception:
            # Игнорируем ошибки, если сообщение не изменилось
            pass

    async def _post_initial_messages(self, channel_id):
        """Создает начальные сообщения и сохраняет их ID"""
        try:
            async for msg in self._client.iter_messages(int(f'-100{channel_id}')):
                await msg.delete()

            history_msg = await self.inline.bot.send_message(
                int(f'-100{channel_id}'),
                self.strings["history_title"],
                parse_mode="HTML",
                disable_web_page_preview=True
            )
            artist_msg = await self.inline.bot.send_message(
                int(f'-100{channel_id}'),
                self.strings["artist_placeholder"]
            )
            self.set("history_msg_id", history_msg.message_id)
            self.set("artist_msg_id", artist_msg.message_id)
            return history_msg.message_id, artist_msg.message_id
        except Exception as e:
            logging.error(f"Не удалось создать начальные сообщения: {e}")
            return None, None

    async def _update_logic(self):
        """Основная логика обновления, вынесенная в отдельный метод"""
        channel_id = self.config["channel_id"]
        if not channel_id:
            if self.get("autochannel"):
                logging.error("ID канала не найден, отключаю модуль.")
                self.set("autochannel", False)
            return
            
        history_msg_id = self.get("history_msg_id")
        artist_msg_id = self.get("artist_msg_id")

        if not history_msg_id or not artist_msg_id:
            logging.warning("ID сообщений не найдены, пытаюсь создать заново...")
            history_msg_id, artist_msg_id = await self._post_initial_messages(channel_id)
            if not history_msg_id:
                logging.error("Не удалось создать сообщения, отключаю модуль.")
                self.set("autochannel", False)
                return

        try:
            track_info = await self.get_current_track()
            now = time.time()

            if not track_info or track_info.get("paused"):
                if self._last_track_title is not None:
                    await self.update_channel_title(channel_id, self.strings["paused_title"])
                    await self._edit_message(channel_id, artist_msg_id, self.strings["artist_placeholder"])
                    self._last_track_title = None
                    self._last_change_ts = now
                return

            track_title = track_info['title']
            if track_title != self._last_track_title:
                artists = utils.escape_html(track_info["artists"])
                
                await self.update_channel_title(channel_id, track_title)
                await self._edit_message(channel_id, artist_msg_id, artists)

                track_history = self.get("track_history", [])
                track_url = f"https://music.yandex.ru/album/{track_info['album_id']}/track/{track_info['id']}"
                new_history_entry = f"<a href=\"{track_url}\">{utils.escape_html(track_title)} - {artists}</a>"

                if not track_history or track_history[-1] != new_history_entry:
                    track_history.append(new_history_entry)

                if len(track_history) > 10:
                    track_history = track_history[-10:]
                
                self.set("track_history", track_history)

                history_text = self.strings["history_title"] + "\n".join(
                    f"<b>{i}.</b> {track}" for i, track in enumerate(reversed(track_history), 1)
                )
                await self._edit_message(channel_id, history_msg_id, history_text)

                self._last_track_title = track_title
                self._last_change_ts = now
                return

            if self._last_change_ts and (now - self._last_change_ts) > 600:
                await self.update_channel_title(channel_id, self.strings["paused_title"])
                await self._edit_message(channel_id, artist_msg_id, self.strings["artist_placeholder"])
                self._last_track_title = None
                self._last_change_ts = now

        except Exception as e:
            logging.error(f"Критическая ошибка в _update_logic: {e}")

    @loader.command(ru_doc="- включить/выключить YaLive")
    async def yalive(self, message):
        """Включение или выключение автоматического обновления"""
        if not self.config["channel_id"]:
            await utils.answer(message, self.strings["channel_id_error"])
            return

        autochannel_status = self.get("autochannel", False)
        new_status = not autochannel_status
        self.set("autochannel", new_status)

        if new_status:
            await self._post_initial_messages(self.config["channel_id"])
            # Вызываем основную логику для мгновенного обновления
            await self._update_logic()

        status_text = "включено" if new_status else "отключено"
        await utils.answer(message, self.strings["on/off"].format(status_text))

    @loader.loop(interval=15, autostart=True)
    async def autochannel_loop(self):
        """Цикл для автоматического обновления информации в канале"""
        if not self.get("autochannel"):
            return
        
        # Просто вызываем основную логику
        await self._update_logic()
