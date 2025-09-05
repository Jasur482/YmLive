update_channel_title(channel_id, self.strings["paused_title"])
                    await self._edit_message(channel_id, artist_msg_id, self.strings["artist_placeholder"])
                    self._last_track_title = None
                    self._last_change_ts = now
                return

            track_title = track_info['title']
            if track_title != self._last_track_title:
                artists = utils.escape_html(track_info["artists"])
                
                logging.info(f"Обновляю трек: {track_title} - {artists}")
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
                logging.info("Таймаут без изменений, устанавливаю паузу")
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
            
        if not self._initialized:
            await utils.answer(message, self.strings["client_not_initialized"])
            return

        autochannel_status = self.get("autochannel", False)
        new_status = not autochannel_status
        self.set("autochannel", new_status)

        if new_status:
            await self._post_initial_messages(self.config["channel_id"])
            await self._update_logic()

        status_text = "включено" if new_status else "отключено"
        await utils.answer(message, self.strings["on/off"].format(status_text))

    @loader.loop(interval=15, autostart=True)
    async def autochannel_loop(self):
        """Цикл для автоматического обновления информации в канале"""
        if not self.get("autochannel"):
            return
        
        await self._update_logic()
