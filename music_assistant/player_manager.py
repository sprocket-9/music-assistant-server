"""PlayerManager: Orchestrates all players from player providers."""

import logging
from datetime import datetime
from typing import Any, List, Optional

from music_assistant.constants import (
    CONF_ENABLED,
    CONF_NAME,
    EVENT_PLAYER_ADDED,
    EVENT_PLAYER_CHANGED,
    EVENT_PLAYER_CONTROL_REGISTERED,
    EVENT_PLAYER_CONTROL_UPDATED,
    EVENT_PLAYER_REMOVED,
)
from music_assistant.models.config_entry import ConfigEntry, ConfigEntryType
from music_assistant.models.media_types import MediaItem, MediaType
from music_assistant.models.player import (
    Player,
    PlayerControl,
    PlayerControlType,
    PlayerState,
)
from music_assistant.models.player_queue import PlayerQueue, QueueItem, QueueOption
from music_assistant.models.playerprovider import PlayerProvider
from music_assistant.models.provider import ProviderType
from music_assistant.utils import (
    async_iter_items,
    callback,
    run_periodic,
    try_parse_int,
)

POLL_INTERVAL = 10
CONF_VOLUME_CONTROL = "volume_control"
CONF_POWER_CONTROL = "power_control"

LOGGER = logging.getLogger("mass")


class PlayerManager:
    """Several helpers to handle playback through player providers."""

    def __init__(self, mass):
        """Initialize class."""
        self.mass = mass
        self._players = {}
        self._org_players = {}
        self._providers = {}
        self._player_queues = {}
        self._poll_ticks = 0
        self._controls = {}
        self._player_controls_config_entries = []

    async def async_setup(self):
        """Async initialize of module."""
        self.mass.add_job(self.poll_task())

    async def async_close(self):
        """Handle stop/shutdown."""
        for player_queue in self._player_queues.values():
            await player_queue.async_close()

    @run_periodic(1)
    async def poll_task(self):
        """Check for updates on players that need to be polled."""
        for player in self._org_players.values():
            if player.should_poll and (
                self._poll_ticks >= POLL_INTERVAL or player.state == PlayerState.Playing
            ):
                # Just request update, value checking for changes is handled
                await self.async_update_player(player)
        if self._poll_ticks >= POLL_INTERVAL:
            self._poll_ticks = 0
        else:
            self._poll_ticks += 1

    @property
    def players(self) -> List[Player]:
        """Return all registered players."""
        return list(self._players.values())

    @property
    def providers(self) -> List[PlayerProvider]:
        """Return all loaded player providers."""
        return self.mass.get_providers(ProviderType.PLAYER_PROVIDER)

    @callback
    def get_player(self, player_id: str) -> Player:
        """Return player by player_id or None if player does not exist."""
        return self._players.get(player_id)

    @callback
    def get_player_provider(self, player_id: str) -> PlayerProvider:
        """Return provider by player_id or None if player does not exist."""
        player = self.get_player(player_id)
        return self.mass.get_provider(player.provider_id) if player else None

    @callback
    def get_player_queue(self, player_id: str) -> PlayerQueue:
        """Return player's queue by player_id or None if player does not exist."""
        if player_id not in self._players:
            return None
        player = self._players[player_id]
        return self._player_queues.get(player.active_queue)

    @callback
    def get_player_control(self, control_id: str) -> PlayerControl:
        """Return PlayerControl by id."""
        if control_id not in self._controls:
            LOGGER.warning("PlayerControl %s is not available", control_id)
            return None
        return self._controls[control_id]

    @callback
    def get_player_controls(
        self, filter_type: Optional[PlayerControlType] = None
    ) -> List[PlayerControl]:
        """Return all PlayerControls, optionally filtered by type."""
        return [
            item
            for item in self._controls.values()
            if (filter_type is None or item.type == filter_type)
        ]

    # ADD/REMOVE/UPDATE HELPERS

    async def async_add_player(self, player: Player) -> None:
        """Register a new player or update an existing one."""
        if not player:
            return
        is_new_player = player.player_id not in self._players
        await self.__async_create_player_state(player)
        if is_new_player:
            # create player queue
            if player.player_id not in self._player_queues:
                self._player_queues[player.player_id] = PlayerQueue(
                    self.mass, player.player_id
                )
            # TODO: turn on player if it was previously turned on ?
            LOGGER.info(
                "New player added: %s/%s",
                player.provider_id,
                self._players[player.player_id].name,
            )
            self.mass.signal_event(EVENT_PLAYER_ADDED, self._players[player.player_id])

    async def async_remove_player(self, player_id: str):
        """Remove a player from the registry."""
        self._players.pop(player_id, None)
        self._org_players.pop(player_id, None)
        LOGGER.info("Player removed: %s", player_id)
        self.mass.signal_event(EVENT_PLAYER_REMOVED, {"player_id": player_id})

    async def async_update_player(self, player: Player):
        """Update an existing player (or register as new if non existing)."""
        if not player:
            return
        if player.player_id not in self._players:
            return await self.async_add_player(player)
        await self.__async_create_player_state(player)

    async def async_register_player_control(self, control: PlayerControl):
        """Register a playercontrol with the player manager."""
        self._controls[control.id] = control
        LOGGER.info("New %s PlayerControl registered: %s", control.type, control.name)
        self.mass.signal_event(EVENT_PLAYER_CONTROL_REGISTERED, control.id)
        await self.__async_create_playercontrol_config_entries()
        # update all players as they may want to use this control
        for player in self._players.values():
            self.mass.add_job(self.async_update_player(player))

    async def async_update_player_control(self, control_id: str, new_state: Any):
        """Update a playercontrol's state on the player manager."""
        control = self._controls.get(control_id)
        if not control or control.state == new_state:
            return
        LOGGER.info("PlayerControl %s updated - new state: %s", control.name, new_state)
        control.state = new_state
        self.mass.signal_event(EVENT_PLAYER_CONTROL_UPDATED, control.id)
        # update all players using this playercontrol
        for player_id, player in self._players.items():
            conf = self.mass.config.player_settings[player_id]
            if control.id in [
                conf.get(CONF_POWER_CONTROL),
                conf.get(CONF_VOLUME_CONTROL),
            ]:
                self.mass.add_job(self.async_update_player(player))

    # SERVICE CALLS / PLAYER COMMANDS

    async def async_play_media(
        self,
        player_id: str,
        media_items: List[MediaItem],
        queue_opt: QueueOption = QueueOption.Play,
    ):
        """
        Play media item(s) on the given player.

            :param player_id: player_id of the player to handle the command.
            :param media_item: media item(s) that should be played (single item or list of items)
            :param queue_opt:
                QueueOption.Play -> Insert new items in queue and start playing at inserted position
                QueueOption.Replace -> Replace queue contents with these items
                QueueOption.Next -> Play item(s) after current playing item
                QueueOption.Add -> Append new items at end of the queue
        """
        player = self._players[player_id]
        if not player:
            return
        # a single item or list of items may be provided
        queue_items = []
        for media_item in media_items:
            # collect tracks to play
            if media_item.media_type == MediaType.Artist:
                tracks = self.mass.music_manager.async_get_artist_toptracks(
                    media_item.item_id, provider_id=media_item.provider
                )
            elif media_item.media_type == MediaType.Album:
                tracks = self.mass.music_manager.async_get_album_tracks(
                    media_item.item_id, provider_id=media_item.provider
                )
            elif media_item.media_type == MediaType.Playlist:
                tracks = self.mass.music_manager.async_get_playlist_tracks(
                    media_item.item_id, provider_id=media_item.provider
                )
            else:
                tracks = async_iter_items(media_item)  # single track
            async for track in tracks:
                queue_item = QueueItem(track)
                # generate uri for this queue item
                queue_item.uri = "%s/stream/%s/%s" % (
                    self.mass.web.internal_url,
                    player_id,
                    queue_item.queue_item_id,
                )
                queue_items.append(queue_item)
        # turn on player
        await self.async_cmd_power_on(player_id)
        # load items into the queue
        player_queue = self.get_player_queue(player_id)
        if queue_opt == QueueOption.Replace or (
            len(queue_items) > 10 and queue_opt in [QueueOption.Play, QueueOption.Next]
        ):
            return await player_queue.async_load(queue_items)
        if queue_opt == QueueOption.Next:
            return await player_queue.async_insert(queue_items, 1)
        if queue_opt == QueueOption.Play:
            return await player_queue.async_insert(queue_items, 0)
        if queue_opt == QueueOption.Add:
            return await player_queue.async_append(queue_items)

    async def async_cmd_stop(self, player_id: str) -> None:
        """
        Send STOP command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        # TODO: redirect playback related commands to parent player?
        return await self.get_player_provider(player_id).async_cmd_stop(player_id)

    async def async_cmd_play(self, player_id: str) -> None:
        """
        Send PLAY command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        # power on at play request
        await self.async_cmd_power_on(player_id)
        player = self.get_player(player_id)
        # unpause if paused else resume queue
        if player.state == PlayerState.Paused:
            return await self.get_player_provider(player_id).async_cmd_play(player_id)
        return await self._player_queues[player_id].async_resume()
        # TODO: redirect playback related commands to parent player?

    async def async_cmd_pause(self, player_id: str):
        """
        Send PAUSE command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        return await self.get_player_provider(player_id).async_cmd_pause(player_id)
        # TODO: redirect playback related commands to parent player?

    async def async_cmd_play_pause(self, player_id: str):
        """
        Toggle play/pause on given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self.get_player(player_id)
        if player.state == PlayerState.Playing:
            return await self.async_cmd_pause(player_id)
        return await self.async_cmd_play(player_id)

    async def async_cmd_next(self, player_id: str):
        """
        Send NEXT TRACK command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        return await self.get_player_queue(player_id).async_next()

    async def async_cmd_previous(self, player_id: str):
        """
        Send PREVIOUS TRACK command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        return await self.get_player_queue(player_id).async_previous()

    async def async_cmd_power_on(self, player_id: str) -> None:
        """
        Send POWER ON command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self._players[player_id]
        player_config = self.mass.config.player_settings[player.player_id]
        # turn on player
        await self.get_player_provider(player_id).async_cmd_power_on(player_id)
        # player control support
        if player_config.get(CONF_POWER_CONTROL):
            control = self.get_player_control(player_config[CONF_POWER_CONTROL])
            if control:
                self.mass.add_job(control.set_state, control.id, True)

    async def async_cmd_power_off(self, player_id: str) -> None:
        """
        Send POWER OFF command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self._players[player_id]
        player_config = self.mass.config.player_settings[player.player_id]
        # turn off player
        await self.get_player_provider(player_id).async_cmd_power_off(player_id)
        # player control support
        if player_config.get(CONF_POWER_CONTROL):
            control = self.get_player_control(player_config[CONF_POWER_CONTROL])
            if control:
                self.mass.add_job(control.set_state, control.id, False)
        # handle group power
        if player.is_group_player:
            # player is group, turn off all childs
            for child_player_id in player.group_childs:
                if self._players.get(child_player_id):
                    await self.async_cmd_power_off(child_player_id)

    async def async_cmd_power_toggle(self, player_id: str):
        """
        Send POWER TOGGLE command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self._players[player_id]
        if player.powered:
            return await self.async_cmd_power_off(player_id)
        return await self.async_cmd_power_on(player_id)

    async def async_cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """
        Send volume level command to given player.

            :param player_id: player_id of the player to handle the command.
            :param volume_level: volume level to set (0..100).
        """
        player = self.get_player(player_id)
        if not player.powered:
            return
        player_prov = self.get_player_provider(player_id)
        player_config = self.mass.config.player_settings[player.player_id]
        volume_level = try_parse_int(volume_level)
        if volume_level < 0:
            volume_level = 0
        elif volume_level > 100:
            volume_level = 100
        # player control support
        if player_config.get(CONF_VOLUME_CONTROL):
            control = self.get_player_control(player_config[CONF_VOLUME_CONTROL])
            if control:
                self.mass.add_job(control.set_state, control.id, volume_level)
                # just force full volume on actual player if volume is outsourced to volumecontrol
                await player_prov.async_cmd_volume_set(player_id, 100)
        # handle group volume
        elif player.is_group_player:
            cur_volume = player.volume_level
            new_volume = volume_level
            volume_dif = new_volume - cur_volume
            if cur_volume == 0:
                volume_dif_percent = 1 + (new_volume / 100)
            else:
                volume_dif_percent = volume_dif / cur_volume
            for child_player_id in player.group_childs:
                child_player = self._players.get(child_player_id)
                if child_player and child_player.available and child_player.powered:
                    cur_child_volume = child_player.volume_level
                    new_child_volume = cur_child_volume + (
                        cur_child_volume * volume_dif_percent
                    )
                    await self.async_cmd_volume_set(child_player_id, new_child_volume)
        # regular volume command
        else:
            await player_prov.async_cmd_volume_set(player_id, volume_level)

    async def async_cmd_volume_up(self, player_id: str):
        """
        Send volume UP command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self._players[player_id]
        new_level = player.volume_level + 1
        if new_level > 100:
            new_level = 100
        return await self.async_cmd_volume_set(player_id, new_level)

    async def async_cmd_volume_down(self, player_id: str):
        """
        Send volume DOWN command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self._players[player_id]
        new_level = player.volume_level - 1
        if new_level < 0:
            new_level = 0
        return await self.async_cmd_volume_set(player_id, new_level)

    async def async_cmd_volume_mute(self, player_id: str, is_muted=False):
        """
        Send MUTE command to given player.

            :param player_id: player_id of the player to handle the command.
            :param is_muted: bool with the new mute state.
        """
        player_prov = self.get_player_provider(player_id)
        # TODO: handle mute on volumecontrol?
        return await player_prov.async_cmd_volume_mute(player_id, is_muted)

    # OTHER/HELPER FUNCTIONS

    async def async_get_gain_correct(
        self, player_id: str, item_id: str, provider_id: str
    ):
        """Get gain correction for given player / track combination."""
        player_conf = self.mass.config.get_player_config(player_id)
        if not player_conf["volume_normalisation"]:
            return 0
        target_gain = int(player_conf["target_volume"])
        fallback_gain = int(player_conf["fallback_gain_correct"])
        track_loudness = await self.mass.database.async_get_track_loudness(
            item_id, provider_id
        )
        if track_loudness is None:
            gain_correct = fallback_gain
        else:
            gain_correct = target_gain - track_loudness
        gain_correct = round(gain_correct, 2)
        LOGGER.debug(
            "Loudness level for track %s/%s is %s - calculated replayGain is %s",
            id,
            item_id,
            track_loudness,
            gain_correct,
        )
        return gain_correct

    async def __async_create_player_state(self, player: Player):
        """Create/update internal Player object with all calculated properties."""
        self._org_players[player.player_id] = player
        player_enabled = bool(
            self.mass.config.get_player_config(player.player_id)[CONF_ENABLED]
        )
        if player.player_id in self._players:
            player_state = self._players[player.player_id]
        else:
            player_state = Player(player.player_id, player.provider_id)
            self._players[player.player_id] = player_state
            setattr(player_state, "_on_update", self.__player_updated)
        group_parents = self.__get_player_group_parents(player)
        active_queue = self.__get_player_active_queue(player, group_parents)
        player_state.name = self.__get_player_name(player)
        player_state.powered = self.__get_player_power_state(player)
        if active_queue != player.player_id:
            player_state.elapsed_time = self._players[active_queue].elapsed_time
            player_state.current_uri = self._players[active_queue].current_uri
        else:
            player_state.elapsed_time = int(player.elapsed_time)
            player_state.current_uri = player.current_uri
        player_state.state = self.__get_player_state(player, active_queue)
        player_state.available = False if not player_enabled else player.available
        player_state.volume_level = self.__get_player_volume_level(player)
        player_state.muted = self.__get_player_mute_state(player)
        player_state.is_group_player = player.is_group_player
        player_state.group_childs = player.group_childs
        player_state.device_info = player.device_info
        player_state.should_poll = player.should_poll
        player_state.features = player.features
        player_state.config_entries = self.__get_player_config_entries(player)
        player_state.active_queue = active_queue
        if active_queue in self._player_queues:
            player_state.cur_queue_item_id = self._player_queues[
                active_queue
            ].cur_item_id

    @callback
    def __get_player_name(self, player: Player):
        """Get final/calculated player name."""
        conf_name = self.mass.config.get_player_config(player.player_id)[CONF_NAME]
        return conf_name if conf_name else player.name

    @callback
    def __get_player_power_state(self, player: Player):
        """Get final/calculated player's power state."""
        if not player.available:
            return False
        player_config = self.mass.config.player_settings[player.player_id]
        if player_config.get(CONF_POWER_CONTROL):
            control = self.get_player_control(player_config[CONF_POWER_CONTROL])
            if control:
                return control.state
        return player.powered

    @callback
    def __get_player_volume_level(self, player: Player):
        """Get final/calculated player's volume_level."""
        if not player.available:
            return 0
        player_config = self.mass.config.player_settings[player.player_id]
        if player_config.get(CONF_VOLUME_CONTROL):
            control = self.get_player_control(player_config[CONF_VOLUME_CONTROL])
            if control:
                return control.state
        # handle group volume
        if player.is_group_player:
            group_volume = 0
            active_players = 0
            for child_player_id in player.group_childs:
                child_player = self._players.get(child_player_id)
                if child_player and child_player.available and child_player.powered:
                    group_volume += child_player.volume_level
                    active_players += 1
            if active_players:
                group_volume = group_volume / active_players
            return group_volume
        return player.volume_level

    @callback
    def __get_player_state(self, player: Player, active_parent: str):
        """Get final/calculated player's state."""
        if not player.available or not player.powered:
            return PlayerState.Off
        if active_parent != player.player_id:
            # use group state
            return self._players[active_parent].state
        return player.state

    @callback
    @classmethod
    def __get_player_mute_state(cls, player: Player):
        """Get final/calculated player's mute state."""
        # TODO: Handle VolumeControl plugin for mute state?
        return player.muted

    @callback
    def __get_player_group_parents(self, player: Player):
        """Return all group players this player belongs to."""
        if player.is_group_player:
            return []
        result = []
        for group_player in self._players.values():
            if not group_player.is_group_player:
                continue
            if player.player_id not in group_player.group_childs:
                continue
            result.append(group_player.player_id)
        return result

    @callback
    def __get_player_active_queue(self, player: Player, group_parents: List[str]):
        """Return the active parent player/queue for a player."""
        # if a group is playing, all of it's childs will have/use
        # the parent's player's queue.
        for group_player_id in group_parents:
            group_player = self.get_player(group_player_id)
            if group_player and group_player.powered:
                return group_player_id
        return player.player_id

    @callback
    def __get_player_config_entries(self, player: Player):
        """Get final/calculated config entries for this player."""
        return player.config_entries + self._player_controls_config_entries

    async def __async_create_playercontrol_config_entries(self):
        """Create special config entries for player controls."""
        entries = []
        # append power control config entries
        power_controls = self.get_player_controls(PlayerControlType.POWER)
        if power_controls:
            controls = [
                {"text": item.name, "value": item.id} for item in power_controls
            ]
            entries.append(
                ConfigEntry(
                    entry_key=CONF_POWER_CONTROL,
                    entry_type=ConfigEntryType.STRING,
                    description_key=CONF_POWER_CONTROL,
                    values=controls,
                )
            )
        # append volume control config entries
        volume_controls = self.get_player_controls(PlayerControlType.VOLUME)
        if volume_controls:
            controls = [
                {"text": item.name, "value": item.id} for item in volume_controls
            ]
            entries.append(
                ConfigEntry(
                    entry_key=CONF_VOLUME_CONTROL,
                    entry_type=ConfigEntryType.STRING,
                    description_key=CONF_VOLUME_CONTROL,
                    values=controls,
                )
            )
        self._player_controls_config_entries = entries

    @callback
    def __player_updated(self, player_id: str, changed_value: str):
        """Call when player is updated."""
        if player_id not in self._players:
            return
        player = self._players[player_id]
        if not player.available and changed_value != "available":
            # ignore updates from unavailable players
            return
        if changed_value == "config_entries":
            return  # we can ignore this too
        # store datetime the player was last updated
        player.updated_at = datetime.utcnow()
        # signal player_updated on all state changes except elapsed time
        if not changed_value == "elapsed_time":
            self.mass.signal_event(EVENT_PLAYER_CHANGED, self._players[player_id])
            # signal child players
            if player.is_group_player:
                for child_player_id in player.group_childs:
                    child_player = self.get_player(child_player_id)
                    if child_player and child_player.available:
                        self.mass.add_job(self.async_update_player(child_player))
        if player_id in self._player_queues and player.active_queue == player_id:
            self.mass.add_job(self._player_queues[player_id].async_update_state())
