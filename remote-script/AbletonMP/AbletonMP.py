# -*- coding: utf-8 -*-
"""AbletonMP -- С‚РѕРЅРєРёР№ bridge РјС–Р¶ Live Object Model С– Р»РѕРєР°Р»СЊРЅРёРј daemon.

Р†РЅРІР°СЂС–Р°РЅС‚ С†СЊРѕРіРѕ С„Р°Р№Р»Сѓ: **Р·РІС–РґСЃРё РЅС–РєРѕР»Рё РЅРµ РІРёР»С–С‚Р°С” РІРёРЅСЏС‚РѕРє Сѓ Live**. РљРѕР¶РµРЅ callback
Р· Р±РѕРєСѓ Live С– РєРѕР¶РµРЅ tick Р·Р°РіРѕСЂРЅСѓС‚С– РІ _safe(). Р’СЃСЏ Р»РѕРіС–РєР°, СЏРєСѓ РјРѕР¶РЅР° РІРёРЅРµСЃС‚Рё РЅР°Р·РѕРІРЅС–,
РІРёРЅРµСЃРµРЅР° РІ daemon.

Р¤Р°Р·Р° 1: transport (play/stop), tempo, clip launch/stop.
"""

import json
import hashlib
import math
import os
import time
import traceback

import Live

try:
    from ableton.v2.control_surface import ControlSurface
except ImportError:  # СЃС‚Р°СЂС–С€С–/С–РЅС€С– Р·Р±С–СЂРєРё
    from _Framework.ControlSurface import ControlSurface

from .link import UdpLink
from .registry import Registry
from . import chat as _chat

try:
    import importlib
    _chat = importlib.reload(_chat)
except Exception:
    pass

AIChatServer = _chat.AIChatServer

SCRIPT_VERSION = "0.19.0-dev"

# Типи, які цей bridge уміє ЗАСТОСУВАТИ. Оголошуються при конекті, щоб розсинхрон
# версій між учасниками (vision.md §8) виявлявся одразу, а не виглядав як
# "синхронізація не працює": подія доходить, але приймальний бік про неї не знає.
APPLY_TYPES = [
    "TransportSet", "TempoSet",
    "ClipLaunch", "ClipStop", "SceneLaunch", "StopAllClips",
    "TrackCreate", "TrackDelete", "SceneCreate", "SceneDelete",
    "MixerSet", "TrackToggle", "DeviceParamSet", "ObjectMetaSet",
    "ClipCreate", "ClipDelete", "ClipNotesSet", "ClipLoopSet",
]
HEARTBEAT_SEC = 2.0
LOG_MAX_BYTES = 512 * 1024

# Р”РµР±Р°СѓРЅСЃ РЅРµРїРµСЂРµСЂРІРЅРёС… РїР°СЂР°РјРµС‚СЂС–РІ: Р¶СѓСЂРЅР°Р» РјР°С” РЅРµСЃС‚Рё РґС–С— РєРѕСЂРёСЃС‚СѓРІР°С‡Р°, Р° РЅРµ РєРѕР¶РµРЅ
# РєСЂРѕРє СЂСѓС‡РєРё. DEBOUNCE_SEC -- С‚РёС€Р° РїС–СЃР»СЏ РѕСЃС‚Р°РЅРЅСЊРѕС— Р·РјС–РЅРё, РїС–СЃР»СЏ СЏРєРѕС— Р¶РµСЃС‚
# РІРІР°Р¶Р°С”С‚СЊСЃСЏ Р·Р°РІРµСЂС€РµРЅРёРј. DEBOUNCE_MAX_HOLD -- СЃС‚РµР»СЏ: РїС–Рґ С‡Р°СЃ РґРѕРІРіРѕРіРѕ Р±РµР·РїРµСЂРµСЂРІРЅРѕРіРѕ
# Р¶РµСЃС‚Сѓ РїРѕРґС–СЏ РІСЃРµ РѕРґРЅРѕ Р№РґРµ СЂР°Р· РЅР° СЃРµРєСѓРЅРґСѓ, С‰РѕР± С…РІРёР»РёРЅРЅРёР№ СЂСѓС… РЅРµ РїСЂРѕРїР°РІ РїСЂРё СЂРѕР·СЂРёРІС–
# (С‚РѕР№ СЃР°РјРёР№ checkpoint, С‰Рѕ Р№ Сѓ vision.md В§5.5).
DEBOUNCE_SEC = 0.2
DEBOUNCE_MAX_HOLD = 1.0

# MIDI notes are synchronized as deterministic fixed regions. A whole-clip
# snapshot can exceed the localhost UDP datagram limit; 4 beats x 16 pitches
# keeps each semantic event bounded while preserving last-global-seq wins for
# overlapping edits.
# Повний стан не влазить в один датаграм, тож іде чанками з паузами по тіках.
# Що вміє цей bridge. Список читає relay при конекті (vision.md §8) і daemon,
# щоб не просити того, чого нема.
FEATURES = ["apply_ack", "ai_chat", "authenticated_lom", "full_state", "state_apply",
            "presence", "view_follow"]

STATE_VERSION = 1
STATE_CHUNK_CHARS = 30000
STATE_CHUNKS_PER_TICK = 6
# Застосування знімка: тисячі записів у LOM порціями, щоб не підвісити Live.
STATE_APPLY_PER_TICK = 12
STATE_APPLY_MAX_BYTES = 64 * 1024 * 1024
NOTES_PER_REGION = 1024
# Скільки різних прогалин несе звіт: далі йде лише лічильник.
MISSING_LIMIT = 50

# Присутність: погляд має бути жвавим, тож дебаунс менший за журнальний.
# VIEW_MAX_HOLD дає стелю у два повідомлення на секунду.
VIEW_DEBOUNCE_SEC = 0.15
VIEW_MAX_HOLD = 0.5
# Скільки після власного запису виду мовчати: Live може доправити виділення
# асинхронно, якщо цільовий обʼєкт саме зник.
VIEW_ECHO_WINDOW = 0.5

# Стеля довжини кліпу. Live під час запису віддає заглушку у два роки секунд
# (63072000), і без цієї межі партнер створює кліп на 63 мільйони долей.
# Вісім годин на 999 bpm -- це ~480 тисяч, тож мільйон покриває реальність
# із запасом і на два порядки менший за заглушку.
CLIP_LENGTH_MAX = 1e6
# Скільки довжина має не мінятись, щоб вважати запис завершеним.
REC_SETTLE_SEC = 0.3
# Loop і маркери йдуть однією подією: Live клампить loop_end відносно
# loop_start, тож окремі події проходили б через невалідні проміжні стани.
CLIP_LOOP_PROPS = ("looping", "loop_start", "loop_end", "start_marker", "end_marker")

NOTE_TIME_SPAN = 4.0
NOTE_PITCH_SPAN = 16
NOTE_FIELDS = (
    "pitch", "start_time", "duration", "velocity", "mute",
    "probability", "velocity_deviation", "release_velocity",
)

# РљР»СЋС‡С– РґР»СЏ set_data/get_data -- Р·Р±РµСЂС–РіР°РЅРЅСЏ РІСЃРµСЂРµРґРёРЅС– СЃР°РјРѕРіРѕ .als.
# РџСЂС–РѕСЂРёС‚РµС‚ Р·Р° DATA_KEY_OBJ: uuid Р»РµР¶РёС‚СЊ РЅР° СЃР°РјРѕРјСѓ РѕР±'С”РєС‚С–, С‚РѕР¶ РїРµСЂРµР¶РёРІР°С”
# РїРµСЂРµСЃС‚Р°РІР»СЏРЅРЅСЏ С‚СЂРµРєС–РІ РјС–Р¶ СЃРµСЃС–СЏРјРё. DATA_KEY_MAP -- С„РѕР»Р±РµРє РѕРґРЅС–С”СЋ РјР°РїРѕСЋ РЅР° Song,
# СЏРєС‰Рѕ РѕР±'С”РєС‚Рё РЅРµ РїС–РґС‚СЂРёРјСѓСЋС‚СЊ set_data; РІС–РЅ РїСЂРёРІ'СЏР·Р°РЅРёР№ РґРѕ РїРѕР·РёС†С–Р№ С– СЃР»Р°Р±С€РёР№.
DATA_KEY_OBJ = "abletonmp_id"
DATA_KEY_MAP = "abletonmp_registry"


def _log_path():
    base = os.environ.get("APPDATA") or os.environ.get("TMPDIR") or "."
    d = os.path.join(base, "AbletonMP")
    try:
        if not os.path.isdir(d):
            os.makedirs(d)
    except Exception:
        return None
    return os.path.join(d, "bridge.log")


class AbletonMP(ControlSurface):
    def __init__(self, c_instance):
        ControlSurface.__init__(self, c_instance)
        self._log_file = _log_path()
        self._doc = None
        self._link = None
        self._chat = None
        self._ai_seq = 0
        self._lseq = 0
        self._last_beat = 0.0
        self._mirror = {
            "playing": None, "tempo": None, "psi": {}, "mix": {},
            "device": {}, "notes": {}, "clips": {}, "meta": {}, "view": None,
            "loop": {},
        }
        self._obj_cbs = []  # (РѕР±'С”РєС‚, РЅР°Р·РІР° РІР»Р°СЃС‚РёРІРѕСЃС‚С–, callback)
        self._pending = {}   # key -> РІС–РґРєР»Р°РґРµРЅР° РїРѕРґС–СЏ, СЃС…Р»РѕРїСѓС”С‚СЊСЃСЏ Р·Р° РєР»СЋС‡РµРј
        self._note_pending = {}  # clip key -> {track, scene, clip, due, first}
        self._rec_pending = {}   # clip key -> кліп, що зараз пишеться
        self._clip_buf = {}  # track_idx -> psi, РЅР°РєРѕРїРёС‡СѓС”С‚СЊСЃСЏ РјС–Р¶ С‚С–РєР°РјРё
        self._unshared_tracks = set()  # групи: uuid є, але в мережу не йдуть
        self._group_warned = set()
        self._view_cbs = []      # підписки на song.view, окремо від _obj_cbs
        self._view_pending = None
        self._suppress_view = False
        self._view_applied_at = 0.0
        self._view_guard_logged = False
        self._state_queue = []   # чанки повного стану, віддаються по тіках
        self._state_id = 0
        self._apply_queue = []   # знімок, розкладений на події
        self._apply_report = None
        self._tracks_reg = Registry(self._log)
        self._scenes_reg = Registry(self._log)
        self._aux_tracks_reg = Registry(self._log)
        self._chains_reg = Registry(self._log)
        self._saved_aux_track_records = []
        self._aux_track_records = []
        self._saved_chain_records = []
        self._chain_records = []
        # РґРѕ Р±СѓС‚СЃС‚СЂР°РїСѓ uuid С‰Рµ РЅРµ СЃРїС–Р»СЊРЅС– Р· РїР°СЂС‚РЅРµСЂРѕРј, С‚РѕР¶ РїРѕРґС–С— Р· РїРѕСЃРёР»Р°РЅРЅСЏРјРё
        # РЅР° РѕР±'С”РєС‚Рё РЅС–РєСѓРґРё РЅРµ РІС–РґРїСЂР°РІР»СЏС”РјРѕ -- РІРѕРЅРё Р± Сѓ РЅСЊРѕРіРѕ РЅРµ Р·Р°СЂРµР·РѕР»РІРёР»РёСЃСЊ
        self._registry_ready = False
        # РїРѕРєРё Р·Р°СЃС‚РѕСЃРѕРІСѓС”РјРѕ С‡СѓР¶Сѓ СЃС‚СЂСѓРєС‚СѓСЂРЅСѓ РїРѕРґС–СЋ, СЃРІС–Р№ listener РјР°С” РјРѕРІС‡Р°С‚Рё:
        # С–РЅР°РєС€Рµ СЃС‚РІРѕСЂРµРЅРёР№ С‚СЂРµРє РѕРґСЂР°Р·Сѓ РїРѕС—С…Р°РІ Р±Рё РЅР°Р·Р°Рґ СЏРє РІР»Р°СЃРЅРёР№ TrackCreate
        self._suppress_struct = False
        self._safe(self._setup)

    # ------------------------------------------------------------------ setup

    def _setup(self):
        self._doc = Live.Application.get_application().get_document()
        self._link = UdpLink(self._log)
        self._chat = AIChatServer(self._log)
        if self._chat.start():
            self._show_message("AbletonMP AI chat: %s token in %s"
                               % (self._chat.url, self._chat.token_source))

        self._doc.add_is_playing_listener(self._cb_is_playing)
        self._doc.add_tempo_listener(self._cb_tempo)
        self._doc.add_tracks_listener(self._cb_tracks)
        self._doc.add_scenes_listener(self._cb_scenes)
        # РїРѕСЏРІР°/Р·РЅРёРєРЅРµРЅРЅСЏ return-С‚СЂРµРєСѓ Р·РјС–РЅСЋС” РєС–Р»СЊРєС–СЃС‚СЊ send-С–РІ РЅР° РєРѕР¶РЅРѕРјСѓ С‚СЂРµРєСѓ,
        # Р° С†Рµ РѕРєСЂРµРјС– listener'Рё -- Р±РµР· С†СЊРѕРіРѕ РЅРѕРІС– send-Рё Р»РёС€РёР»РёСЃСЊ Р±Рё РЅС–РјРёРјРё
        self._doc.add_return_tracks_listener(self._cb_tracks)
        self._rewire_tracks()
        self._wire_view()
        self._prime_mirror()

        self._link.send({
            "m": "hello",
            "live": self._live_version(),
            "script": SCRIPT_VERSION,
            "pid": os.getpid(),
            "events": APPLY_TYPES,
            "features": FEATURES,
        })
        self._link.send({"m": "snapshot", "state": self._snapshot()})
        self._log("AbletonMP %s connected, Live %s" % (SCRIPT_VERSION, self._live_version()))
        self._safe(self._probe_persistence)

    def _probe_persistence(self):
        """Р©Рѕ РґРѕСЃС‚СѓРїРЅРѕ РґР»СЏ Р·Р±РµСЂС–РіР°РЅРЅСЏ СЂРµС”СЃС‚СЂСѓ РІ С†С–Р№ Р·Р±С–СЂС†С– Live.

        РќС–С‡РѕРіРѕ РЅРµ РїРёС€Рµ -- Р»РёС€Рµ РґРёРІРёС‚СЊСЃСЏ. Р”СЂСѓРіР° РјР°С€РёРЅР° РјРѕР¶Рµ РјР°С‚Рё С–РЅС€Сѓ РІРµСЂСЃС–СЋ Live,
        С– С‚РѕРґС– С†РµР№ СЂСЏРґРѕРє Сѓ Р»РѕР·С– РѕРґСЂР°Р·Сѓ РїРѕСЏСЃРЅСЋС”, С‡РѕРјСѓ СЂРµС”СЃС‚СЂ РЅРµ РїРµСЂРµР¶РёРІ СЃРµСЃС–СЋ.
        """
        caps = {
            "song.set_data": hasattr(self._doc, "set_data"),
            "track.set_data": bool(self._doc.tracks) and hasattr(self._doc.tracks[0], "set_data"),
            "scene.set_data": bool(self._doc.scenes) and hasattr(self._doc.scenes[0], "set_data"),
            "return_track.set_data": bool(self._doc.return_tracks) and
                                     hasattr(self._doc.return_tracks[0], "set_data"),
            "master_track.set_data": hasattr(self._doc.master_track, "set_data"),
        }
        try:
            caps["file_path"] = str(self._doc.file_path) or "(РЅРµ Р·Р±РµСЂРµР¶РµРЅРѕ)"
        except Exception:
            caps["file_path"] = "(РЅРµРґРѕСЃС‚СѓРїРЅРѕ)"
        self._log("persistence: %r" % (caps,))
        self._link.send({"m": "log", "level": "info", "text": "persistence: %r" % (caps,)})

    def disconnect(self):
        self._safe(self._teardown)
        ControlSurface.disconnect(self)

    def _teardown(self):
        # РЅРµР·Р°РІРµСЂС€РµРЅРёР№ Р¶РµСЃС‚ РЅРµ РјР°С” РїСЂРѕРїР°СЃС‚Рё СЂР°Р·РѕРј С–Р· Р·Р°РєСЂРёС‚С‚СЏРј Live
        self._safe(self._flush_clips)
        self._safe(self._flush_notes, True)
        self._safe(self._flush_pending, True)
        self._unwire_tracks()
        self._unwire_view()
        if self._doc is not None:
            for name, cb in (("is_playing", self._cb_is_playing),
                             ("tempo", self._cb_tempo),
                             ("tracks", self._cb_tracks),
                             ("scenes", self._cb_scenes),
                             ("return_tracks", self._cb_tracks)):
                try:
                    if getattr(self._doc, "%s_has_listener" % name)(cb):
                        getattr(self._doc, "remove_%s_listener" % name)(cb)
                except Exception:
                    pass
        if self._link is not None:
            self._link.send({"m": "bye"})
            self._link.close()
        if self._chat is not None:
            self._chat.stop()
            self._chat = None
        self._log("AbletonMP disconnected")

    # ------------------------------------------------------------- listeners

    def _listen(self, obj, prop, cb, store=None):
        """РЈР·Р°РіР°Р»СЊРЅРµРЅР° РїС–РґРїРёСЃРєР°: LOM С‚СЂРёРјР°С” С”РґРёРЅСѓ СЃС…РµРјСѓ add_/remove_/_has_listener,
        С‚РѕР¶ РїРµСЂРµР»С–С‡СѓРІР°С‚Рё РєРѕР¶РµРЅ РїР°СЂР°РјРµС‚СЂ РѕРєСЂРµРјРѕ РЅРµ С‚СЂРµР±Р°."""
        if store is None:
            store = self._obj_cbs
        try:
            getattr(obj, "add_%s_listener" % prop)(cb)
            store.append((obj, prop, cb))
        except Exception:
            pass  # РїР°СЂР°РјРµС‚СЂР° С‚СѓС‚ РЅРµРјР°С” (РЅР°РїСЂ. arm РЅР° С‚СЂРµРєСѓ, СЏРєРёР№ РЅРµ РѕР·Р±СЂРѕСЋС”С‚СЊСЃСЏ)

    def _rewire_tracks(self):
        self._unwire_tracks()
        for track in self._doc.tracks:
            self._listen(track, "playing_slot_index", self._make_slot_cb(track))
            self._wire_metadata("track", track, track=track)
            self._wire_mixer(track)
            self._wire_devices(track)
            self._wire_note_slots(track)
        for track in self._device_aux_tracks():
            self._wire_metadata("track", track, track=track)
            self._wire_mixer(track)
            self._wire_devices(track)
        for scene in self._doc.scenes:
            self._wire_metadata("scene", scene, scene=scene)

    def _wire_note_slots(self, track):
        try:
            slots = list(track.clip_slots)
            scenes = list(self._doc.scenes)
        except Exception:
            return
        for i, slot in enumerate(slots):
            if i >= len(scenes):
                break
            scene = scenes[i]
            self._listen(slot, "has_clip", self._make_slot_content_cb(track, scene, slot))
            try:
                if slot.has_clip:
                    clip = slot.clip
                    self._wire_metadata("clip", clip, track=track, scene=scene)
                    loop_cb = self._make_clip_loop_cb(track, scene, clip)
                    for prop in CLIP_LOOP_PROPS:
                        self._listen(clip, prop, loop_cb)
                    if clip.is_midi_clip:
                        self._listen(clip, "notes", self._make_notes_cb(track, scene, clip))
            except Exception:
                pass

    def _wire_mixer(self, track):
        for param, idx in self._mix_slots(track):
            p = self._mix_param(track, param, idx)
            if p is not None:
                self._listen(p, "value", self._make_mix_cb(track, param, idx))
        for prop in self._toggle_props(track):
            self._listen(track, prop, self._make_toggle_cb(track, prop))

    def _wire_metadata(self, kind, obj, track=None, scene=None):
        for prop in ("name", "color"):
            self._listen(obj, prop, self._make_metadata_cb(
                kind, obj, prop, track=track, scene=scene))

    def _wire_devices(self, track):
        """Observe parameters recursively through Rack chains."""
        self._wire_device_container(track, track, 0)

    def _wire_device_container(self, track, container, depth):
        if depth > 16:
            self._warn("device tree is deeper than 16 levels; nested observers truncated")
            return
        self._listen(container, "devices", self._make_devices_cb())
        try:
            devices = list(container.devices)
        except Exception:
            return
        for device in devices:
            # Some LOM variants expose the mixer through Track.devices. Mixer has
            # its own event types, so observing it here would duplicate changes.
            if self._device_signature(device) is None:
                continue
            try:
                parameters = list(device.parameters)
            except Exception:
                continue
            for parameter in parameters:
                self._listen(parameter, "value",
                             self._make_device_param_cb(track, device, parameter))
            if not self._device_has_chains(device):
                continue
            for kind, chains in self._rack_chain_groups(device):
                self._listen(device, kind, self._make_devices_cb())
                for chain in chains:
                    self._wire_device_container(track, chain, depth + 1)

    def _unwire_tracks(self):
        for obj, prop, cb in self._obj_cbs:
            try:
                if getattr(obj, "%s_has_listener" % prop)(cb):
                    getattr(obj, "remove_%s_listener" % prop)(cb)
            except Exception:
                pass  # РѕР±'С”РєС‚ СѓР¶Рµ РІРёРґР°Р»РµРЅРёР№ -- Р·РІРµСЂС‚Р°РЅРЅСЏ РєРёРґР°С” RuntimeError
        self._obj_cbs = []

    def _make_slot_cb(self, track):
        def cb():
            self._safe(self._on_playing_slot, track)
        return cb

    def _make_mix_cb(self, track, param, idx):
        def cb():
            self._safe(self._on_mix, track, param, idx)
        return cb

    def _make_toggle_cb(self, track, prop):
        def cb():
            self._safe(self._on_toggle, track, prop)
        return cb

    def _make_metadata_cb(self, kind, obj, prop, track=None, scene=None):
        def cb():
            self._safe(self._on_metadata, kind, obj, prop, track, scene)
        return cb

    def _make_devices_cb(self):
        def cb():
            self._safe(self._on_devices)
        return cb

    def _make_device_param_cb(self, track, device, parameter):
        def cb():
            self._safe(self._on_device_param, track, device, parameter)
        return cb

    def _make_slot_content_cb(self, track, scene, slot):
        def cb():
            self._safe(self._on_slot_content, track, scene, slot)
        return cb

    def _make_notes_cb(self, track, scene, clip):
        def cb():
            self._safe(self._on_notes, track, scene, clip)
        return cb

    def _cb_is_playing(self):
        self._safe(self._on_is_playing)

    def _cb_tempo(self):
        self._safe(self._on_tempo)

    def _cb_tracks(self):
        self._safe(self._on_tracks)

    def _cb_scenes(self):
        self._safe(self._on_scenes)

    def _on_is_playing(self):
        playing = bool(self._doc.is_playing)
        if self._mirror["playing"] == playing:
            return  # С†Рµ РІС–РґР»СѓРЅРЅСЏ РЅР°С€РѕРіРѕ РІР»Р°СЃРЅРѕРіРѕ apply
        self._mirror["playing"] = playing
        self._emit("TransportSet", {"playing": playing})

    def _on_tempo(self):
        bpm = round(float(self._doc.tempo), 6)
        if self._mirror["tempo"] == bpm:
            return
        self._mirror["tempo"] = bpm
        self._defer("tempo", "TempoSet", {"bpm": bpm})

    def _on_tracks(self):
        # СЃС‚СЂСѓРєС‚СѓСЂР° С‚СЂРµРєС–РІ Р·РјС–РЅРёР»Р°СЃСЊ: РїРµСЂРµРїС–РґРїРёСЃСѓС”РјРѕСЃСЊ С– СЃРєРёРґР°С”РјРѕ РґР·РµСЂРєР°Р»Рѕ СЃР»РѕС‚С–РІ,
        # С–РЅР°РєС€Рµ Р·СЃСѓРІ С–РЅРґРµРєСЃС–РІ РїРѕСЂРѕРґРёС‚СЊ С„Р°РЅС‚РѕРјРЅС– ClipLaunch
        self._flush_notes(True)
        self._rewire_tracks()
        self._mirror["psi"] = {}
        self._clip_buf = {}  # РЅР°РєРѕРїРёС‡РµРЅРµ РїРѕСЃРёР»Р°С”С‚СЊСЃСЏ РЅР° СЃС‚Р°СЂС– С–РЅРґРµРєСЃРё
        self._prime_mirror(transport=False)
        if self._registry_ready:
            self._diff_tracks(emit=not self._suppress_struct)
            aux_changed = self._refresh_aux_tracks()
            if self._refresh_chains() or aux_changed:
                self._persist_registry()
            self._prime_mixer()  # listener'Рё РјС–РєС€РµСЂР° РїРµСЂРµРІС–С€Р°РЅС– РЅР° РЅРѕРІС– РѕР±'С”РєС‚Рё
            self._prime_devices()
            self._prime_notes()
            self._prime_metadata()
            self._prime_clip_loops()

            # Виділення могло переїхати разом зі структурою: адреса в підписі
            # уже інша, навіть якщо користувач нічого не чіпав.
            self._touch_view()

    def _on_scenes(self):
        if self._registry_ready:
            self._flush_notes(True)
            self._diff_scenes(emit=not self._suppress_struct)
            self._rewire_tracks()
            self._prime_mirror(transport=False)
            self._prime_mixer()
            self._prime_devices()
            self._prime_notes()
            self._prime_metadata()
            self._prime_clip_loops()

            # Виділення могло переїхати разом зі структурою: адреса в підписі
            # уже інша, навіть якщо користувач нічого не чіпав.
            self._touch_view()

    def _on_devices(self):
        """Rebind observers after any Track/Chain/Rack structure change."""
        changed = self._refresh_chains()
        self._rewire_tracks()
        if self._registry_ready:
            # Device structure is not an event yet. Treat its current parameter
            # values as the new baseline rather than emitting a synthetic burst.
            self._prime_devices()
            if changed:
                self._persist_registry()

    @staticmethod
    def _norm_psi(value):
        """Live РјР°С” РєС–Р»СЊРєР° РІС–РґКјС”РјРЅРёС… Р·РЅР°С‡РµРЅСЊ РґР»СЏ В«РЅРµ РіСЂР°С”В» (-1 РЅС–С‡РѕРіРѕ, -2 С” fired slot).
        Р”Р»СЏ Р¶СѓСЂРЅР°Р»Сѓ С†Рµ РѕРґРёРЅ СЃС‚Р°РЅ; Р±РµР· РЅРѕСЂРјР°Р»С–Р·Р°С†С–С— РїРµСЂРµС…С–Рґ -1 -> -2 РІРёРіР»СЏРґР°С” СЏРє
        Р·СѓРїРёРЅРєР° РєР»С–РїСѓ С– РїРѕСЂРѕРґР¶СѓС” С„Р°РЅС‚РѕРјРЅРёР№ ClipStop."""
        if value is None or value < 0:
            return -1
        return value

    def _on_playing_slot(self, track):
        idx = self._track_index(track)
        if idx is None:
            return
        psi = self._norm_psi(track.playing_slot_index)
        if self._mirror["psi"].get(idx) == psi:
            return
        self._mirror["psi"][idx] = psi
        # РќРµ РІС–РґРїСЂР°РІР»СЏС”РјРѕ РѕРґСЂР°Р·Сѓ: Р·Р°РїСѓСЃРє СЃС†РµРЅРё СЃРјРёРєР°С” listener РЅР° РєРѕР¶РЅРѕРјСѓ С‚СЂРµРєСѓ
        # РѕРєСЂРµРјРѕ. РќР°РєРѕРїРёС‡СѓС”РјРѕ РґРѕ РЅР°СЃС‚СѓРїРЅРѕРіРѕ С‚С–РєСѓ С– С‚Р°Рј РІРёСЂС–С€СѓС”РјРѕ, С‰Рѕ С†Рµ Р±СѓР»Рѕ.
        self._clip_buf[idx] = psi

    # -------------------------------------------------------------- registry

    def _build_registry(self):
        """Р’РёРґР°С” uuid СѓСЃС–Рј РѕР±'С”РєС‚Р°Рј. Р РµР·СѓР»СЊС‚Р°С‚ СЃС‚Р°С” РїРѕРґС–С”СЋ RegistryInit Сѓ Р¶СѓСЂРЅР°Р»С–."""
        self._tracks_reg.clear()
        self._scenes_reg.clear()
        self._aux_tracks_reg.clear()
        self._chains_reg.clear()
        # СЃРїРµСЂС€Сѓ РїС–РґРЅС–РјР°С”РјРѕ uuid С–Р· СЃР°РјРѕРіРѕ .als: СЏРєС‰Рѕ РѕР±РёРґРІС– РјР°С€РёРЅРё РІС–РґРєСЂРёР»Рё С‚РѕР№
        # СЃР°РјРёР№ С„Р°Р№Р», РІРѕРЅРё РѕС‚СЂРёРјР°СЋС‚СЊ РѕРґРЅР°РєРѕРІС– uuid С‰Рµ РґРѕ Р±СѓРґСЊ-СЏРєРѕРіРѕ РѕР±РјС–РЅСѓ
        restored = self._restore_registry()
        reg = {"tracks": [], "scenes": []}
        for i, t in enumerate(self._doc.tracks):
            reg["tracks"].append({"id": self._tracks_reg.id_of(t), "idx": i, "name": self._safe_name(t)})
        for i, s in enumerate(self._doc.scenes):
            reg["scenes"].append({"id": self._scenes_reg.id_of(s), "idx": i, "name": self._safe_name(s)})
        self._refresh_aux_tracks()
        reg["aux_tracks"] = list(self._aux_track_records)
        self._refresh_chains()
        reg["chains"] = list(self._chain_records)
        self._registry_ready = True
        self._safe(self._touch_view, True)  # партнер має одразу побачити, де я
        self._rewire_tracks()
        self._prime_mixer()
        self._prime_devices()
        self._prime_notes()
        self._prime_metadata()
        self._prime_clip_loops()
        self._persist_registry()
        self._log("registry created: %d tracks, %d scenes, %d aux tracks, %d Rack chains (%d ids restored)"
                  % (len(reg["tracks"]), len(reg["scenes"]), len(reg["aux_tracks"]),
                     len(reg["chains"]), restored))
        return reg

    def _adopt_registry(self, reg):
        """РќР°РєР»Р°РґР°С” С‡СѓР¶С– uuid РЅР° СЃРІРѕС— РѕР±'С”РєС‚Рё Р·Р° РїРѕР·РёС†С–С”СЋ, Р·РІС–СЂСЏСЋС‡Рё С–РјРµРЅР°.

        Р¦Рµ С”РґРёРЅРµ РјС–СЃС†Рµ, РґРµ С–РЅРґРµРєСЃ С‰Рµ С” Р°РґСЂРµСЃРѕСЋ -- РѕРґРЅРѕСЂР°Р·РѕРІРѕ, РЅР° Р±СѓС‚СЃС‚СЂР°РїС–.
        Р”Р°Р»С– С–РЅРґРµРєСЃРё РІ РїСЂРѕС‚РѕРєРѕР»С– РЅРµ С„С–РіСѓСЂСѓСЋС‚СЊ РІР·Р°РіР°Р»С–.
        """
        self._tracks_reg.clear()
        self._scenes_reg.clear()
        self._aux_tracks_reg.clear()
        self._chains_reg.clear()
        # uuid, Р·Р±РµСЂРµР¶РµРЅС– РІ .als, РіРѕР»РѕРІРЅС–С€С– Р·Р° РїРѕР·РёС†С–СЋ: С–Рј'СЏ С‚СЂРµРєСѓ РІ Live
        # Р·РјС–РЅСЋС”С‚СЊСЃСЏ СЃР°РјРµ СЃРѕР±РѕСЋ РІС–Рґ РєРёРЅСѓС‚РѕРіРѕ РґРµРІР°Р№СЃР°, С‚РѕР¶ Р·РІС–СЂРєР° Р·Р° С–РјРµРЅРµРј
        # РІС–РґРєРёРґР°Р»Р° Р± С†С–Р»РєРѕРј Р»РµРіС–С‚РёРјРЅС– Р·Р±С–РіРё
        self._restore_registry()
        problems = []
        by_data = 0
        by_position = 0
        matched = {}  # Р·Р° РІРёРґРѕРј: СЃРєС–Р»СЊРєРё Р·Р°РїРёСЃС–РІ Р±СѓР»Рѕ С– СЃРєС–Р»СЊРєРё Р·С–Р№С€Р»РѕСЃСЊ

        for kind, records, objects, reg_obj in (
            ("С‚СЂРµРє", reg.get("tracks") or [], self._doc.tracks, self._tracks_reg),
            ("СЃС†РµРЅР°", reg.get("scenes") or [], self._doc.scenes, self._scenes_reg),
        ):
            matched[kind] = [len(records), 0]
            for rec in records:
                uid = rec.get("id")
                if uid and reg_obj.obj_of(uid) is not None:
                    by_data += 1
                    matched[kind][1] += 1
                    continue  # С†РµР№ РѕР±'С”РєС‚ СѓР¶Рµ РІРїС–Р·РЅР°РІ СЃРµР±Рµ СЃР°Рј

                i = rec.get("idx")
                if not isinstance(i, int) or i < 0 or i >= len(objects):
                    problems.append("%s %r: РїРѕР·РёС†С–С— %r С‚СѓС‚ РЅРµРјР°С”" % (kind, rec.get("name"), i))
                    continue
                want = rec.get("name")
                if want and self._safe_name(objects[i]) != want:
                    problems.append("%s %d: С‚СѓС‚ %r, Сѓ РїР°СЂС‚РЅРµСЂР° %r"
                                    % (kind, i, self._safe_name(objects[i]), want))
                    continue
                reg_obj.bind(uid, objects[i])
                by_position += 1
                matched[kind][1] += 1

        preferred_aux = reg.get("aux_tracks") or []
        self._refresh_aux_tracks(preferred_aux)
        if preferred_aux:
            resolved_aux_ids = set(rec.get("id") for rec in self._aux_track_records)
            missing_aux = [rec for rec in preferred_aux if rec.get("id") not in resolved_aux_ids]
            if missing_aux:
                problems.append("aux tracks unresolved: %d" % len(missing_aux))

        preferred_chains = reg.get("chains") or []
        self._refresh_chains(preferred_chains)
        if preferred_chains:
            resolved_chain_ids = set(rec.get("id") for rec in self._chain_records)
            missing_chains = [rec for rec in preferred_chains
                              if rec.get("id") not in resolved_chain_ids]
            if missing_chains:
                problems.append("Rack chains unresolved: %d" % len(missing_chains))

        self._registry_ready = True
        self._safe(self._touch_view, True)  # партнер має одразу побачити, де я
        self._rewire_tracks()
        self._prime_mixer()
        self._prime_devices()
        self._prime_notes()
        self._prime_metadata()
        self._prime_clip_loops()
        # РєР°РЅРѕРЅС–С‡РЅС– uuid С–Р· Р¶СѓСЂРЅР°Р»Сѓ Р»СЏРіР°СЋС‚СЊ Сѓ .als, С‰РѕР± РЅР°СЃС‚СѓРїРЅРѕРіРѕ СЂР°Р·Сѓ РїСЂРѕС”РєС‚
        # РІС–РґРєСЂРёРІСЃСЏ РІР¶Рµ Р· РЅРёРјРё С– Р±СѓС‚СЃС‚СЂР°Рї Р·Р° РїРѕР·РёС†С–СЏРјРё РЅРµ Р·РЅР°РґРѕР±РёРІСЃСЏ
        self._persist_registry()
        self._log("registry adopted: %d tracks, %d scenes, %d aux tracks, %d Rack chains "
                  "(%d stored, %d by position)"
                  % (len(self._tracks_reg), len(self._scenes_reg), len(self._aux_track_records),
                     len(self._chain_records), by_data, by_position))
        if problems:
            # РїСЂРѕС”РєС‚Рё СЂРѕР·С–Р№С€Р»РёСЃСЊ; РїРѕРґС–С— РЅР° РЅРµР·С–СЃС‚Р°РІР»РµРЅС– РѕР±'С”РєС‚Рё РїСЂРѕСЃС‚Рѕ РЅРµ Р·Р°СЃС‚РѕСЃСѓСЋС‚СЊСЃСЏ
            self._warn("Р±СѓС‚СЃС‚СЂР°Рї СЂРµС”СЃС‚СЂСѓ, РЅРµР·С–СЃС‚Р°РІР»РµРЅРѕ %d: %s"
                       % (len(problems), "; ".join(problems[:5])))
        # Р Р°С…СѓС”РјРѕ РѕРєСЂРµРјРѕ РїРѕ РІРёРґР°С…: СЃС†РµРЅРё С‡Р°СЃС‚Рѕ Р·С–СЃС‚Р°РІР»СЏСЋС‚СЊСЃСЏ РЅР°РІС–С‚СЊ Сѓ С‡СѓР¶РѕРјСѓ
        # РїСЂРѕС”РєС‚С– (С—С… Р°РґСЂРµСЃСѓС” РїРѕР·РёС†С–СЏ РІ РјР°РїС–), С– СЃСѓРјР°СЂРЅРёР№ Р»С–С‡РёР»СЊРЅРёРє С†Рµ РјР°СЃРєСѓРІР°РІ Р±Рё.
        # РќСѓР»СЊ С‚СЂРµРєС–РІ -- С†Рµ РїРѕРІРЅР° РЅС–РјРѕС‚Р°: Р±РµР· uuid С‚СЂРµРєР° Р¶РѕРґРЅР° РїРѕРґС–СЏ РјС–РєС€РµСЂР°,
        # РєР»С–РїР° С‡Рё СЃС‚СЂСѓРєС‚СѓСЂРё РЅРµ РјР°С” Р°РґСЂРµСЃРё.
        for kind, (total, ok) in matched.items():
            if total and not ok:
                self._warn("Р–РћР”Р•Рќ %s РЅРµ Р·С–СЃС‚Р°РІРёРІСЃСЏ (%d Сѓ СЃРµСЃС–С—) -- С†РµР№ РїСЂРѕС”РєС‚ РЅРµ С‚РѕР№, "
                           "С‰Рѕ РІ СЃРµСЃС–С— relay. РџРѕРґС–С— РїРѕ %sС… РїСЂР°С†СЋРІР°С‚Рё РЅРµ Р±СѓРґСѓС‚СЊ; "
                           "РІС–РґРєСЂРёР№ С‚РѕР№ СЃР°РјРёР№ .als Р°Р±Рѕ Р·Р°РІРµРґРё РЅРѕРІСѓ --session"
                           % (kind, total, kind))

    # ----------------------------------------------------- persistence (.als)

    def _obj_stored_id(self, obj):
        try:
            return self._doc_str(obj.get_data(DATA_KEY_OBJ, "")) or None
        except Exception:
            return None

    def _obj_store_id(self, obj, uid):
        try:
            obj.set_data(DATA_KEY_OBJ, uid)
            return True
        except Exception:
            return False

    def _doc_str(self, v):
        return v if isinstance(v, str) else ""

    def _restore_registry(self):
        """РџС–РґРЅС–РјР°С” uuid, Р·Р±РµСЂРµР¶РµРЅС– РІ .als. РџРѕРІРµСЂС‚Р°С” РєС–Р»СЊРєС–СЃС‚СЊ РІС–РґРЅРѕРІР»РµРЅРёС….

        РћР±РёРґРІР° РјРµС…Р°РЅС–Р·РјРё РїСЂР°С†СЋСЋС‚СЊ СЂР°Р·РѕРј, РЅРµ Р·Р°РјС–СЃС‚СЊ РѕРґРЅРѕРіРѕ: Сѓ Live 12 С‚СЂРµРє С‚СЂРёРјР°С”
        set_data, Р° СЃС†РµРЅР° -- РЅС–, С‚РѕР¶ С‡Р°СЃС‚РёРЅР° РѕР±'С”РєС‚С–РІ РІРїС–Р·РЅР°С” СЃРµР±Рµ СЃР°РјР°, Р° СЂРµС€С‚Сѓ
        РґРѕРІРѕРґРёС‚СЊСЃСЏ РґС–СЃС‚Р°РІР°С‚Рё Р· РјР°РїРё РЅР° Song. Р Р°РЅРЅС–Р№ РІРёС…С–Рґ РїС–СЃР»СЏ РїРµСЂС€РѕРіРѕ РїСЂРѕС…РѕРґСѓ
        Р·Р°Р»РёС€Р°РІ Р±Рё СЃС†РµРЅРё Р±РµР· С–РґРµРЅС‚РёС‡РЅРѕСЃС‚С– РЅР°Р·Р°РІР¶РґРё.
        """
        by_object = 0
        for reg, objects in ((self._tracks_reg, self._doc.tracks),
                             (self._scenes_reg, self._doc.scenes),
                             (self._aux_tracks_reg, self._device_aux_tracks())):
            for obj in objects:
                uid = self._obj_stored_id(obj)
                if not uid:
                    continue
                if reg.taken_by_other(uid, obj):
                    # Дубль обʼєкта приніс чужий id разом із set_data.
                    # Хай отримає свій у _diff_tracks, а не краде цей.
                    self._log("збережений id %s уже зайнятий, дубль дістане новий" % uid)
                    continue
                reg.bind(uid, obj)
                by_object += 1

        try:
            raw = self._doc_str(self._doc.get_data(DATA_KEY_MAP, ""))
            saved = json.loads(raw) if raw else None
        except Exception:
            saved = None
        self._saved_chain_records = list((saved or {}).get("chains") or [])
        self._saved_aux_track_records = list((saved or {}).get("aux_tracks") or [])

        by_map = 0
        if saved:
            for key, reg, objects in (("tracks", self._tracks_reg, self._doc.tracks),
                                      ("scenes", self._scenes_reg, self._doc.scenes)):
                for rec in (saved.get(key) or []):
                    i = rec.get("idx")
                    if not isinstance(i, int) or not (0 <= i < len(objects)):
                        continue
                    if reg.id_of(objects[i], create=False):
                        continue  # РѕР±'С”РєС‚ СѓР¶Рµ РІРїС–Р·РЅР°РІ СЃРµР±Рµ С‡РµСЂРµР· set_data
                    if rec.get("name") and self._safe_name(objects[i]) != rec["name"]:
                        continue
                    reg.bind(rec.get("id"), objects[i])
                    by_map += 1

            saved_aux = {}
            for rec in self._saved_aux_track_records:
                if rec.get("id"):
                    saved_aux[self._aux_locator_key(rec.get("kind"), rec.get("idx"),
                                                    rec.get("name"))] = rec["id"]
            for kind, idx, obj in self._iter_aux_tracks():
                if self._aux_tracks_reg.id_of(obj, create=False):
                    continue
                uid = saved_aux.get(self._aux_locator_key(kind, idx, self._safe_name(obj)))
                if uid:
                    self._aux_tracks_reg.bind(uid, obj)
                    by_map += 1

        if by_object or by_map:
            self._log("Р· .als РІС–РґРЅРѕРІР»РµРЅРѕ %d uuid (%d РЅР° РѕР±'С”РєС‚Р°С…, %d Р· РјР°РїРё)"
                      % (by_object + by_map, by_object, by_map))
        return by_object + by_map

    def _persist_registry(self):
        """РљР»Р°РґРµ РїРѕС‚РѕС‡РЅС– uuid Сѓ .als. РЎРµС‚ РїРѕР·РЅР°С‡Р°С”С‚СЊСЃСЏ Р·РјС–РЅРµРЅРёРј -- С†Рµ РѕС‡С–РєСѓРІР°РЅРѕ.

        РњР°РїР° РЅР° Song РїРёС€РµС‚СЊСЃСЏ Р·Р°РІР¶РґРё, Р° РЅРµ Р»РёС€Рµ РєРѕР»Рё РїРµСЂ-РѕР±'С”РєС‚РЅРёР№ Р·Р°РїРёСЃ СѓРїР°РІ:
        Scene.set_data РЅРµ РєРёРґР°С” РІРёРЅСЏС‚РєСѓ, Р°Р»Рµ Р№ РЅРµ РґРѕР¶РёРІР°С” РґРѕ РЅР°СЃС‚СѓРїРЅРѕРіРѕ РІС–РґРєСЂРёС‚С‚СЏ
        С„Р°Р№Р»Сѓ, С‚РѕР¶ РґРµС‚РµРєС‚СѓРІР°С‚Рё РїСЂРѕР±Р»РµРјСѓ РїРѕ exception РЅРµ РјРѕР¶РЅР°.
        """
        for reg, objects in ((self._tracks_reg, self._doc.tracks),
                             (self._scenes_reg, self._doc.scenes),
                             (self._aux_tracks_reg, self._device_aux_tracks())):
            for obj in objects:
                uid = reg.id_of(obj, create=False)
                if uid:
                    self._obj_store_id(obj, uid)

        for rec in self._chain_records:
            chain = self._chains_reg.obj_of(rec.get("id"))
            if chain is not None:
                self._obj_store_id(chain, rec["id"])

        snap = {"tracks": [], "scenes": [], "aux_tracks": list(self._aux_track_records),
                "chains": list(self._chain_records)}
        for key, reg, objects in (("tracks", self._tracks_reg, self._doc.tracks),
                                  ("scenes", self._scenes_reg, self._doc.scenes)):
            for i, obj in enumerate(objects):
                uid = reg.id_of(obj, create=False)
                if uid:
                    snap[key].append({"id": uid, "idx": i, "name": self._safe_name(obj)})
        try:
            self._doc.set_data(DATA_KEY_MAP, json.dumps(snap))
        except Exception as e:
            self._warn("СЂРµС”СЃС‚СЂ РЅРµ Р·Р±РµСЂРµР¶РµРЅРѕ РІ .als: %r" % (e,))

    def _iter_aux_tracks(self):
        """Yield stable locator parts for Return tracks and the Master track."""
        try:
            for idx, track in enumerate(self._doc.return_tracks):
                yield "return", idx, track
        except Exception:
            pass
        try:
            master = self._doc.master_track
            if master is not None:
                yield "master", 0, master
        except Exception:
            pass

    def _device_aux_tracks(self):
        return [track for _kind, _idx, track in self._iter_aux_tracks()]

    @staticmethod
    def _aux_locator_key(kind, idx, name):
        # Master is a singleton. Its UI name can be localized, so only the kind
        # belongs to its migration locator. Return tracks need position + name.
        locator = {"kind": kind}
        if kind == "return":
            locator["idx"] = idx
            locator["name"] = name or ""
        return json.dumps(locator, sort_keys=True, ensure_ascii=True,
                          separators=(",", ":"))

    def _refresh_aux_tracks(self, preferred_records=None):
        """Assign stable IDs to Return/Master device containers.

        New RegistryInit records are canonical. Old sessions migrate through
        per-object/Song persistence and finally a deterministic structural ID.
        """
        old_records = json.dumps(self._aux_track_records, sort_keys=True,
                                 ensure_ascii=True, separators=(",", ":"))
        preferred = {}
        for rec in (preferred_records or []):
            if rec.get("id"):
                preferred[self._aux_locator_key(rec.get("kind"), rec.get("idx"),
                                                rec.get("name"))] = rec["id"]
        saved = {}
        for rec in self._saved_aux_track_records:
            if rec.get("id"):
                saved[self._aux_locator_key(rec.get("kind"), rec.get("idx"),
                                            rec.get("name"))] = rec["id"]

        records = []
        live_ids = set()
        for kind, idx, track in self._iter_aux_tracks():
            name = self._safe_name(track)
            locator_key = self._aux_locator_key(kind, idx, name)
            current = self._aux_tracks_reg.id_of(track, create=False)
            uid = (preferred.get(locator_key) or current or self._obj_stored_id(track) or
                   saved.get(locator_key))
            if not uid:
                uid = hashlib.sha256(locator_key.encode("utf-8")).hexdigest()[:12]
            if uid != current:
                self._aux_tracks_reg.bind(uid, track)
            rec = {"id": uid, "kind": kind, "idx": idx, "name": name}
            records.append(rec)
            live_ids.add(uid)

        for uid in self._aux_tracks_reg.known_ids():
            if uid not in live_ids:
                self._aux_tracks_reg.forget(uid)
        self._aux_track_records = records
        new_records = json.dumps(records, sort_keys=True, ensure_ascii=True,
                                 separators=(",", ":"))
        return old_records != new_records

    def _aux_kind_of(self, track):
        for kind, _idx, candidate in self._iter_aux_tracks():
            try:
                if candidate == track:
                    return kind
            except Exception:
                pass
        return None

    def _device_track_ref(self, track):
        tid = self._tracks_reg.id_of(track, create=False)
        if tid:
            # Один чокпоїнт замість гарду в кожному емітері: порожня адреса
            # глушить MixerSet, TrackToggle, DeviceParamSet і ObjectMetaSet.
            return None if tid in self._unshared_tracks else {"id": tid}
        aid = self._aux_tracks_reg.id_of(track, create=False)
        kind = self._aux_kind_of(track)
        if aid and kind:
            return {"id": aid, "kind": kind}
        return None

    def _group_of(self, track):
        """Назва групи, у якій лежить трек. None -- трек поза групою.

        Порівнювати uuid груп між машинами немає сенсу: група в партнера
        не існує як спільний обʼєкт. А от факт «цей трек у групі, а в тебе ні»
        і її назва -- саме те, що людина впізнає.
        """
        try:
            parent = track.group_track
        except Exception:
            return None
        if parent is None:
            return None
        return {"name": self._safe_name(parent)}

    def _iter_device_tracks(self):
        for track in self._doc.tracks:
            yield track
        for track in self._device_aux_tracks():
            yield track

    def _is_group_track(self, track):
        """Group Track. LOM не вміє їх створювати, тож ми їх не анонсуємо."""
        try:
            return bool(track.is_foldable)
        except Exception:
            return False

    def _track_kind(self, track):
        if self._is_group_track(track):
            return "group"   # лише для діагностики: у подію це не потрапляє
        try:
            return "midi" if track.has_midi_input else "audio"
        except Exception:
            return "audio"

    def _warn_group_once(self, uid, track):
        if uid in self._group_warned:
            return
        self._group_warned.add(uid)
        self._warn(
            "Group Track %r не синхронізується: LOM не вміє групувати треки. "
            "Створи групу вручну на обох машинах з тими самими треками -- "
            "значення всередині неї синхронізуються далі." % (self._safe_name(track),))

    def _diff_tracks(self, emit=True):
        """Р—РІС–СЂСЏС” СЂРµС”СЃС‚СЂ С–Р· РґРµСЂРµРІРѕРј С‚СЂРµРєС–РІ РїС–СЃР»СЏ Р·РјС–РЅРё СЃС‚СЂСѓРєС‚СѓСЂРё."""
        created, removed = self._tracks_reg.diff(self._doc.tracks)
        for uid in removed:
            self._tracks_reg.forget(uid)
        if created or removed:
            self._persist_registry()
        if not emit:
            return
        for uid, idx, track in created:
            if self._is_group_track(track):
                # uuid усе одно виданий і збережений: без нього нічим адресувати
                # навіть діагностику. А от анонсувати нема чого -- у партнера
                # група не створиться, і TrackCreate дав би йому фантом.
                self._unshared_tracks.add(uid)
                self._warn_group_once(uid, track)
                continue
            ref = {"id": uid, "name": self._safe_name(track)}
            color = self._safe_color(track)
            if color is not None:
                ref["color"] = color
            self._emit("TrackCreate", {
                "track": ref,
                "idx": idx,
                "kind": self._track_kind(track),
            })
        for uid in removed:
            if uid in self._unshared_tracks:
                # Не анонсували створення -- не анонсуємо й зникнення
                self._unshared_tracks.discard(uid)
                continue
            self._emit("TrackDelete", {"track": {"id": uid}})

    def _diff_scenes(self, emit=True):
        created, removed = self._scenes_reg.diff(self._doc.scenes)
        for uid in removed:
            self._scenes_reg.forget(uid)
        if created or removed:
            self._persist_registry()
        if not emit:
            return
        for uid, idx, scene in created:
            ref = {"id": uid}
            name = self._safe_name(scene)
            if name:
                ref["name"] = name
            color = self._safe_color(scene)
            if color is not None:
                ref["color"] = color
            self._emit("SceneCreate", {"scene": ref, "idx": idx})
        for uid in removed:
            self._emit("SceneDelete", {"scene": {"id": uid}})

    # --------------------------------------------------------------- metadata

    @staticmethod
    def _safe_color(obj):
        try:
            value = int(obj.color)
            return value if 0 <= value <= 0xFFFFFF else None
        except Exception:
            return None

    def _metadata_address(self, kind, obj, track=None, scene=None):
        if kind == "track":
            ref = self._device_track_ref(track if track is not None else obj)
            return {"object": kind, "track": ref} if ref else None
        if kind == "scene":
            target = scene if scene is not None else obj
            uid = self._scenes_reg.id_of(target, create=False)
            return {"object": kind, "scene": {"id": uid}} if uid else None
        if kind == "clip" and track is not None and scene is not None:
            refs = self._clip_refs(track, scene)
            if refs["track"].get("id") and refs["scene"].get("id"):
                refs["object"] = kind
                return refs
        return None

    @staticmethod
    def _metadata_key(address, prop):
        return "%s:%s" % (json.dumps(address, sort_keys=True, ensure_ascii=True,
                                      separators=(",", ":")), prop)

    def _metadata_value(self, obj, prop):
        if prop == "name":
            return self._safe_name(obj)
        if prop == "color":
            return self._safe_color(obj)
        return None

    def _on_metadata(self, kind, obj, prop, track=None, scene=None):
        if not self._registry_ready or self._suppress_struct:
            return
        address = self._metadata_address(kind, obj, track, scene)
        value = self._metadata_value(obj, prop)
        if address is None or value is None:
            return
        key = self._metadata_key(address, prop)
        if self._mirror["meta"].get(key) == value:
            return
        self._mirror["meta"][key] = value
        payload = dict(address)
        payload.update({"prop": prop, "value": value})
        self._emit("ObjectMetaSet", payload)
        if prop == "name" and kind in ("track", "scene"):
            target_track = track if track is not None else obj
            if kind == "track" and self._aux_kind_of(target_track):
                self._refresh_aux_tracks()
            self._persist_registry()

    # ----------------------------------------------------------------- mixer

    def _mix_param(self, track, param, idx):
        if (param, idx) not in self._mix_slots(track):
            return None
        try:
            md = track.mixer_device
            if param == "volume":
                return md.volume
            if param == "panning":
                return md.panning
            if param == "crossfader":
                return md.crossfader
            if param == "cue_volume":
                return md.cue_volume
            if param == "send":
                sends = list(md.sends)
                return sends[idx]
        except Exception:
            pass
        return None

    @staticmethod
    def _mix_track_key(track_ref):
        if not isinstance(track_ref, dict):
            return str(track_ref)
        kind = track_ref.get("kind")
        uid = track_ref.get("id")
        return "%s:%s" % (kind, uid) if kind else str(uid)

    def _mix_key(self, track_ref, param, idx):
        return "%s:%s:%s" % (self._mix_track_key(track_ref), param, idx)

    def _toggle_key(self, track_ref, prop):
        return "%s:%s" % (self._mix_track_key(track_ref), prop)

    def _on_mix(self, track, param, idx):
        if not self._registry_ready:
            return
        track_ref = self._device_track_ref(track)
        p = self._mix_param(track, param, idx)
        if not track_ref or p is None:
            return
        value = round(float(p.value), 6)
        key = self._mix_key(track_ref, param, idx)
        if self._mirror["mix"].get(key) == value:
            return
        self._mirror["mix"][key] = value
        payload = {"track": track_ref, "param": param, "value": value}
        if idx is not None:
            payload["index"] = idx
        # РЅРµРїРµСЂРµСЂРІРЅР° РІРµР»РёС‡РёРЅР° -- РґРµР±Р°СѓРЅСЃРёРјРѕ, СЏРє tempo: СЂСѓС… С„РµР№РґРµСЂР° С†Рµ РѕРґРёРЅ Р¶РµСЃС‚
        self._defer("mix:" + key, "MixerSet", payload)

    def _on_toggle(self, track, prop):
        if not self._registry_ready:
            return
        track_ref = self._device_track_ref(track)
        if not track_ref or prop not in self._toggle_props(track):
            return
        try:
            value = bool(getattr(track, prop))
        except Exception:
            return
        key = self._toggle_key(track_ref, prop)
        if self._mirror["mix"].get(key) == value:
            return
        self._mirror["mix"][key] = value
        # РґРёСЃРєСЂРµС‚РЅРµ РїРµСЂРµРјРёРєР°РЅРЅСЏ -- РґРµР±Р°СѓРЅСЃ С‚СѓС‚ Р»РёС€Рµ РґРѕРґР°РІ Р±Рё Р·Р°С‚СЂРёРјРєРё
        self._emit("TrackToggle", {"track": track_ref, "param": prop, "value": value})

    # ------------------------------------------------------------- devices

    @staticmethod
    def _device_signature(device):
        """Return the stable part of a device address within its container.

        ``name`` is user-editable, while class_display_name is the original
        device/plugin name. MixerDevice is handled by MixerSet/TrackToggle.
        """
        try:
            class_name = str(device.class_name)
            display_name = str(device.class_display_name)
        except Exception:
            return None
        if not class_name or class_name == "MixerDevice":
            return None
        return class_name, display_name

    @staticmethod
    def _device_parameter_name(parameter):
        # original_name keeps a Rack Macro address stable after the user renames
        # it. For ordinary/plugin parameters Live may expose only name.
        try:
            name = str(parameter.original_name)
            if name:
                return name
        except Exception:
            pass
        try:
            return str(parameter.name)
        except Exception:
            return ""

    def _device_ref(self, container, device):
        signature = self._device_signature(device)
        if signature is None:
            return None
        ordinal = 0
        try:
            devices = list(container.devices)
        except Exception:
            return None
        found = False
        for candidate in devices:
            if candidate == device:
                found = True
                break
            if self._device_signature(candidate) == signature:
                ordinal += 1
        if not found:
            return None
        return {
            "class_name": signature[0],
            "class_display_name": signature[1],
            "ordinal": ordinal,
        }

    def _device_parameter_ref(self, device, parameter):
        name = self._device_parameter_name(parameter)
        if not name:
            return None
        ordinal = 0
        try:
            parameters = list(device.parameters)
        except Exception:
            return None
        found = False
        for candidate in parameters:
            if candidate == parameter:
                found = True
                break
            if self._device_parameter_name(candidate) == name:
                ordinal += 1
        if not found:
            return None
        return {"name": name, "ordinal": ordinal}

    @staticmethod
    def _device_has_chains(device):
        try:
            return bool(device.can_have_chains)
        except Exception:
            return False

    @staticmethod
    def _rack_chain_groups(device):
        groups = []
        for kind in ("chains", "return_chains"):
            try:
                groups.append((kind, list(getattr(device, kind))))
            except Exception:
                pass
        return groups

    def _chain_locator(self, track_ref, parent_id, container, rack, kind, idx, chain):
        locator = {
            "track": track_ref.get("id"),
            "parent_chain": parent_id,
            "rack": self._device_ref(container, rack),
            "kind": kind,
            "idx": idx,
            "name": self._safe_name(chain),
        }
        if track_ref.get("kind"):
            locator["track_kind"] = track_ref["kind"]
        return locator

    @staticmethod
    def _chain_locator_key(locator):
        return json.dumps(locator, sort_keys=True, ensure_ascii=True,
                          separators=(",", ":"))

    def _free_id(self, reg, obj, *candidates):
        """Перший кандидат, який ще не зайнятий іншим живим об'єктом."""
        for uid in candidates:
            if uid and not reg.taken_by_other(uid, obj):
                return uid
        return None

    def _refresh_chains(self, preferred_records=None):
        """Discover all Rack chains and assign stable session UUIDs.

        Canonical RegistryInit records win during adopt. Otherwise an ID stored
        on Chain/Song wins, with a deterministic structural hash as migration
        fallback for sessions whose original RegistryInit predates chain IDs.
        """
        old_records = json.dumps(self._chain_records, sort_keys=True, ensure_ascii=True,
                                 separators=(",", ":"))
        preferred = {}
        for rec in (preferred_records or []):
            locator = {key: rec.get(key) for key in
                       ("track", "parent_chain", "rack", "kind", "idx", "name")}
            if rec.get("track_kind"):
                locator["track_kind"] = rec["track_kind"]
            if rec.get("id"):
                preferred[self._chain_locator_key(locator)] = rec["id"]
        saved = {}
        for rec in self._saved_chain_records:
            locator = {key: rec.get(key) for key in
                       ("track", "parent_chain", "rack", "kind", "idx", "name")}
            if rec.get("track_kind"):
                locator["track_kind"] = rec["track_kind"]
            if rec.get("id"):
                saved[self._chain_locator_key(locator)] = rec["id"]

        records = []
        changed = False

        def walk(track, container, parent_id, depth):
            nonlocal changed
            if depth > 16:
                return
            try:
                devices = list(container.devices)
            except Exception:
                return
            track_ref = self._device_track_ref(track)
            if not track_ref:
                return
            for rack in devices:
                if not self._device_has_chains(rack):
                    continue
                for kind, chains in self._rack_chain_groups(rack):
                    for idx, chain in enumerate(chains):
                        locator = self._chain_locator(
                            track_ref, parent_id, container, rack, kind, idx, chain)
                        if locator["rack"] is None:
                            continue
                        locator_key = self._chain_locator_key(locator)
                        uid = self._chains_reg.id_of(chain, create=False)
                        if uid is None:
                            uid = self._free_id(
                                self._chains_reg, chain,
                                preferred.get(locator_key),
                                self._obj_stored_id(chain),
                                saved.get(locator_key))
                            if not uid:
                                uid = hashlib.sha256(locator_key.encode("utf-8")).hexdigest()[:12]
                            self._chains_reg.bind(uid, chain)
                            changed = True
                        rec = dict(locator)
                        rec["id"] = uid
                        records.append(rec)
                        walk(track, chain, uid, depth + 1)

        for track in self._iter_device_tracks():
            walk(track, track, None, 0)
        self._chain_records = records
        new_records = json.dumps(records, sort_keys=True, ensure_ascii=True,
                                 separators=(",", ":"))
        return changed or old_records != new_records

    def _iter_track_devices(self, track):
        """Yield (container, device, chain_path) for the entire device tree."""
        def walk(container, chain_path, depth):
            if depth > 16:
                return
            try:
                devices = list(container.devices)
            except Exception:
                return
            for device in devices:
                if self._device_signature(device) is None:
                    continue
                yield container, device, chain_path
                if not self._device_has_chains(device):
                    continue
                for _kind, chains in self._rack_chain_groups(device):
                    for chain in chains:
                        cid = self._chains_reg.id_of(chain, create=False)
                        if cid:
                            for item in walk(chain, chain_path + [{"id": cid}], depth + 1):
                                yield item

        for item in walk(track, [], 0):
            yield item

    def _device_location(self, track, target):
        for container, device, chain_path in self._iter_track_devices(track):
            try:
                if device == target:
                    return container, self._device_ref(container, device), chain_path
            except Exception:
                pass
        return None, None, None

    def _chain_belongs_to(self, container, chain):
        try:
            devices = list(container.devices)
        except Exception:
            return False
        for rack in devices:
            if not self._device_has_chains(rack):
                continue
            for _kind, chains in self._rack_chain_groups(rack):
                for candidate in chains:
                    try:
                        if candidate == chain:
                            return True
                    except Exception:
                        pass
        return False

    @staticmethod
    def _device_key(track_ref, chain_path, device_ref, parameter_ref):
        return json.dumps([
            track_ref,
            chain_path,
            device_ref.get("class_name"),
            device_ref.get("class_display_name"),
            device_ref.get("ordinal"),
            parameter_ref.get("name"),
            parameter_ref.get("ordinal"),
        ], ensure_ascii=True, separators=(",", ":"))

    def _resolve_device_parameter(self, track, chain_path, device_ref, parameter_ref):
        if not isinstance(device_ref, dict) or not isinstance(parameter_ref, dict):
            return None, None
        if chain_path is None:
            chain_path = []
        if not isinstance(chain_path, list) or len(chain_path) > 16:
            return None, None
        container = track
        for chain_ref in chain_path:
            if not isinstance(chain_ref, dict) or not isinstance(chain_ref.get("id"), str):
                return None, None
            chain = self._chains_reg.obj_of(chain_ref["id"])
            if chain is None or not self._chain_belongs_to(container, chain):
                return None, None
            container = chain
        class_name = device_ref.get("class_name")
        display_name = device_ref.get("class_display_name")
        device_ordinal = device_ref.get("ordinal")
        parameter_name = parameter_ref.get("name")
        parameter_ordinal = parameter_ref.get("ordinal")
        if (not isinstance(class_name, str) or not isinstance(display_name, str) or
                not isinstance(device_ordinal, int) or device_ordinal < 0 or
                not isinstance(parameter_name, str) or
                not isinstance(parameter_ordinal, int) or parameter_ordinal < 0):
            return None, None
        try:
            devices = [
                device for device in container.devices
                if self._device_signature(device) == (class_name, display_name)
            ]
        except Exception:
            return None, None
        if device_ordinal >= len(devices):
            return None, None
        device = devices[device_ordinal]
        try:
            parameters = [
                parameter for parameter in device.parameters
                if self._device_parameter_name(parameter) == parameter_name
            ]
        except Exception:
            return device, None
        if parameter_ordinal >= len(parameters):
            return device, None
        return device, parameters[parameter_ordinal]

    def _on_device_param(self, track, device, parameter):
        if not self._registry_ready:
            return
        track_ref = self._device_track_ref(track)
        _container, device_ref, chain_path = self._device_location(track, device)
        parameter_ref = self._device_parameter_ref(device, parameter)
        if not track_ref or device_ref is None or chain_path is None or parameter_ref is None:
            return
        try:
            value = float(parameter.value)
        except Exception:
            return
        if math.isnan(value) or math.isinf(value):
            return
        value = round(value, 6)
        key = self._device_key(track_ref, chain_path, device_ref, parameter_ref)
        if self._mirror["device"].get(key) == value:
            return
        self._mirror["device"][key] = value

        # Active automation changes value on every playback tick. State 2 is a
        # user's manual override and should be synchronized as an intentional edit.
        try:
            if int(parameter.automation_state) == 1 or not bool(parameter.is_enabled):
                return
        except Exception:
            pass

        payload = {
            "track": track_ref,
            "device": device_ref,
            "parameter": parameter_ref,
            "value": value,
        }
        if chain_path:
            payload["chain_path"] = chain_path
        try:
            quantized = bool(parameter.is_quantized)
        except Exception:
            quantized = False
        if quantized:
            self._emit("DeviceParamSet", payload)
        else:
            self._defer("device:" + key, "DeviceParamSet", payload)

    # ------------------------------------------------------------- MIDI clips

    def _clip_key(self, track, scene):
        if not self._registry_ready:
            return None
        tid = self._tracks_reg.id_of(track, create=False)
        sid = self._scenes_reg.id_of(scene, create=False)
        return "%s:%s" % (tid, sid) if tid and sid else None

    def _clip_refs(self, track, scene):
        return {
            "track": {"id": self._tracks_reg.id_of(track, create=False)},
            "scene": {"id": self._scenes_reg.id_of(scene, create=False)},
        }

    def _clip_meta(self, clip):
        try:
            length = max(0.001, float(clip.length))
        except Exception:
            length = NOTE_TIME_SPAN
        result = {"length": round(length, 6), "name": self._safe_name(clip)}
        color = self._safe_color(clip)
        if color is not None:
            result["color"] = color
        return result

    def _on_slot_content(self, track, scene, slot):
        """Track clip creation/deletion and rebind the note observer."""
        key = self._clip_key(track, scene)
        if key is None:
            self._rewire_tracks()
            return
        previous = self._mirror["clips"].get(key)
        current = None
        clip = None
        try:
            if slot.has_clip:
                clip = slot.clip
                current = "midi" if clip.is_midi_clip else "audio"
        except Exception:
            current = None

        # Кліп, який зараз пишеться, ще не подія: Live віддає заглушкову
        # довжину, і партнер створив би кліп на два роки.
        if clip is not None and self._clip_is_recording(slot, clip):
            self._park_recording(key, track, scene, slot)
            return
        self._rec_pending.pop(key, None)

        # A note callback may still be waiting in the debounce queue when the
        # clip is deleted or replaced. It must not follow ClipDelete with a
        # stale ClipNotesSet that would recreate the clip on the peer.
        self._note_pending.pop(key, None)
        self._mirror["clips"][key] = current
        if previous != current and not self._suppress_struct:
            refs = self._clip_refs(track, scene)
            if current == "midi":
                payload = dict(refs)
                payload["clip"] = self._clip_meta(clip)
                self._emit("ClipCreate", payload)
            elif current is None and previous is not None:
                self._emit("ClipDelete", refs)
            elif current == "audio":
                self._warn("audio clip creation is not synchronized; collect the sample and copy the .als structure")

        # has_clip changed: the old clip listener is dead or a new one is needed.
        self._rewire_tracks()
        self._prime_metadata()
        self._prime_clip_loops()
        if current == "midi" and clip is not None:
            notes = self._clip_notes(clip)
            self._mirror["notes"][key] = notes
            if previous != current and not self._suppress_struct:
                self._emit_all_note_regions(track, scene, clip, notes)
        else:
            self._mirror["notes"].pop(key, None)

    def _on_notes(self, track, scene, clip):
        """Coalesce a piano-roll gesture before calculating changed regions."""
        key = self._clip_key(track, scene)
        if key is None or key in self._rec_pending:
            return  # запис триває: ноти поїдуть разом із готовим кліпом
        now = time.time()
        previous = self._note_pending.get(key)
        self._note_pending[key] = {
            "track": track,
            "scene": scene,
            "clip": clip,
            "due": now + DEBOUNCE_SEC,
            "first": previous["first"] if previous else now,
        }

    def _flush_notes(self, force=False):
        now = time.time()
        for key in list(self._note_pending.keys()):
            pending = self._note_pending[key]
            if not force and now < pending["due"] and now - pending["first"] < DEBOUNCE_MAX_HOLD:
                continue
            del self._note_pending[key]
            try:
                current = self._clip_notes(pending["clip"])
            except Exception:
                continue
            previous = self._mirror["notes"].get(key)
            self._mirror["notes"][key] = current
            if previous is None:
                continue  # a newly discovered clip is a baseline, not a local edit
            regions = self._changed_note_regions(previous, current)
            for region in sorted(regions):
                self._emit_note_region(
                    pending["track"], pending["scene"], pending["clip"], current, region)

    def _emit_all_note_regions(self, track, scene, clip, notes):
        regions = set(self._note_region(note) for note in notes)
        for region in sorted(regions):
            self._emit_note_region(track, scene, clip, notes, region)

    def _emit_note_region(self, track, scene, clip, notes, region):
        from_pitch, pitch_span, from_time, time_span = region
        payload = self._clip_refs(track, scene)
        payload["clip"] = self._clip_meta(clip)
        payload["region"] = {
            "from_pitch": from_pitch,
            "pitch_span": pitch_span,
            "from_time": from_time,
            "time_span": time_span,
        }
        payload["notes"] = self._notes_in_region(notes, region)
        self._emit("ClipNotesSet", payload)

    def _changed_note_regions(self, before, after):
        old_counts = self._note_counts(before)
        new_counts = self._note_counts(after)
        regions = set()
        samples = {}
        for note in before + after:
            samples[self._note_signature(note)] = note
        for signature in set(old_counts) | set(new_counts):
            if old_counts.get(signature, 0) != new_counts.get(signature, 0):
                regions.add(self._note_region(samples[signature]))
        return regions

    def _note_region(self, note):
        pitch = int(note["pitch"])
        start = float(note["start_time"])
        from_pitch = (pitch // NOTE_PITCH_SPAN) * NOTE_PITCH_SPAN
        from_time = math.floor(start / NOTE_TIME_SPAN) * NOTE_TIME_SPAN
        return (from_pitch, min(NOTE_PITCH_SPAN, 128 - from_pitch),
                round(from_time, 6), NOTE_TIME_SPAN)

    def _notes_in_region(self, notes, region):
        return sorted([
            note for note in notes
            if self._note_in_region(note, region)
        ], key=self._note_signature)

    @staticmethod
    def _note_in_region(note, region):
        from_pitch, pitch_span, from_time, time_span = region
        return (from_pitch <= note["pitch"] < from_pitch + pitch_span
                and from_time <= note["start_time"] < from_time + time_span)

    def _note_counts(self, notes):
        counts = {}
        for note in notes:
            signature = self._note_signature(note)
            counts[signature] = counts.get(signature, 0) + 1
        return counts

    @staticmethod
    def _note_signature(note):
        return tuple(note[field] for field in NOTE_FIELDS)

    @staticmethod
    def _note_value(note, field, default):
        if isinstance(note, dict):
            return note.get(field, default)
        return getattr(note, field, default)

    def _normal_note(self, note):
        try:
            out = {
                "pitch": int(self._note_value(note, "pitch", -1)),
                "start_time": round(float(self._note_value(note, "start_time", 0.0)), 6),
                "duration": round(float(self._note_value(note, "duration", 0.0)), 6),
                "velocity": round(float(self._note_value(note, "velocity", 100.0)), 6),
                "mute": bool(self._note_value(note, "mute", False)),
                "probability": round(float(self._note_value(note, "probability", 1.0)), 6),
                "velocity_deviation": round(float(
                    self._note_value(note, "velocity_deviation", 0.0)), 6),
                "release_velocity": round(float(
                    self._note_value(note, "release_velocity", 64.0)), 6),
            }
        except Exception:
            return None
        numeric = [out[field] for field in NOTE_FIELDS if field not in ("pitch", "mute")]
        if not all(math.isfinite(value) for value in numeric):
            return None
        if not (0 <= out["pitch"] <= 127 and out["duration"] > 0
                and 0 <= out["velocity"] <= 127
                and 0 <= out["probability"] <= 1
                and -127 <= out["velocity_deviation"] <= 127
                and 0 <= out["release_velocity"] <= 127):
            return None
        return out

    def _raw_notes(self, clip):
        try:
            return list(clip.get_all_notes_extended())
        except Exception:
            return list(clip.get_notes_extended(0, 128, -1073741824.0, 2147483648.0))

    def _clip_note_records(self, clip):
        records = []
        for raw in self._raw_notes(clip):
            note = self._normal_note(raw)
            if note is None:
                continue
            note_id = self._note_value(raw, "note_id", None)
            records.append((note, note_id))
        return records

    def _clip_notes(self, clip):
        return sorted([note for note, _note_id in self._clip_note_records(clip)],
                      key=self._note_signature)

    def _prime_notes(self):
        """Capture clip presence and note baselines without emitting startup events."""
        self._note_pending = {}
        self._mirror["notes"] = {}
        self._mirror["clips"] = {}
        if not self._registry_ready:
            return
        scenes = list(self._doc.scenes)
        for track in self._doc.tracks:
            try:
                slots = list(track.clip_slots)
            except Exception:
                continue
            for i, scene in enumerate(scenes):
                if i >= len(slots):
                    break
                slot = slots[i]
                key = self._clip_key(track, scene)
                if key is None:
                    continue
                try:
                    if not slot.has_clip:
                        self._mirror["clips"][key] = None
                        continue
                    clip = slot.clip
                    if self._clip_is_recording(slot, clip):
                        self._park_recording(key, track, scene, slot)
                        continue
                    self._rec_pending.pop(key, None)
                    kind = "midi" if clip.is_midi_clip else "audio"
                    self._mirror["clips"][key] = kind
                    if kind == "midi":
                        self._mirror["notes"][key] = self._clip_notes(clip)
                except Exception:
                    pass

    # ------------------------------------------------------ loop і маркери

    def _make_clip_loop_cb(self, track, scene, clip):
        def cb():
            self._safe(self._on_clip_loop, track, scene, clip)
        return cb

    def _clip_loop_state(self, clip):
        """Усі пʼять полів разом. None -- кліп їх не має (напр. не-warped audio)."""
        state = {}
        for prop in CLIP_LOOP_PROPS:
            try:
                value = getattr(clip, prop)
            except Exception:
                continue
            if prop == "looping":
                state[prop] = bool(value)
                continue
            try:
                value = round(float(value), 6)
            except Exception:
                continue
            if not math.isfinite(value) or abs(value) > CLIP_LENGTH_MAX:
                return None
            state[prop] = value
        return state or None

    def _on_clip_loop(self, track, scene, clip):
        if not self._registry_ready or self._suppress_struct:
            return
        key = self._clip_key(track, scene)
        if key is None or key in self._rec_pending:
            return  # під запис межі ще заглушкові
        state = self._clip_loop_state(clip)
        if state is None or self._mirror["loop"].get(key) == state:
            return
        self._mirror["loop"][key] = state
        payload = self._clip_refs(track, scene)
        payload.update(state)
        # Спільний ключ на всі пʼять полів: тягнення брекета смикає loop_start
        # і loop_end десятки разів, а жест має дати одну подію.
        self._defer("loop:" + key, "ClipLoopSet", payload)

    def _prime_clip_loop(self, track, scene, slot):
        key = self._clip_key(track, scene)
        if key is None:
            return
        try:
            if not slot.has_clip:
                self._mirror["loop"].pop(key, None)
                return
            state = self._clip_loop_state(slot.clip)
        except Exception:
            return
        if state is None:
            self._mirror["loop"].pop(key, None)
        else:
            self._mirror["loop"][key] = state

    def _prime_clip_loops(self):
        """Без прайму перший же рух брекета виглядав би як зміна відносно None."""
        self._mirror["loop"] = {}
        try:
            scenes = list(self._doc.scenes)
        except Exception:
            return
        for track in self._doc.tracks:
            try:
                slots = list(track.clip_slots)
            except Exception:
                continue
            for i, scene in enumerate(scenes):
                if i >= len(slots):
                    break
                self._prime_clip_loop(track, scene, slots[i])

    def _prime_note_clip(self, track, scene, slot):
        key = self._clip_key(track, scene)
        if key is None:
            return
        try:
            if not slot.has_clip:
                self._mirror["clips"][key] = None
                self._mirror["notes"].pop(key, None)
                return
            clip = slot.clip
            if self._clip_is_recording(slot, clip):
                # Інакше перепідписка серед запису зробила б кліп базовою
                # лінією, і подія не пішла б уже ніколи.
                self._park_recording(key, track, scene, slot)
                return
            self._rec_pending.pop(key, None)
            kind = "midi" if clip.is_midi_clip else "audio"
            self._mirror["clips"][key] = kind
            if kind == "midi":
                self._mirror["notes"][key] = self._clip_notes(clip)
            else:
                self._mirror["notes"].pop(key, None)
        except Exception:
            pass

    # ------------------------------------------------------- кліп під запис

    def _clip_is_recording(self, slot, clip):
        """Кліп, який зараз пишеться. Два незалежні критерії навмисно.

        Прапорець може поводитись по-різному в різних збірках Live, а от
        довжина під час запису -- це завжди заглушка: у журналі живої сесії
        лежать 63072000 (два роки в секундах) і 63017064.
        """
        for obj, prop in ((clip, "is_recording"), (clip, "is_overdubbing"),
                          (slot, "is_recording")):
            try:
                if getattr(obj, prop):
                    return True
            except Exception:
                pass
        return not self._clip_length_sane(clip)

    @staticmethod
    def _clip_length_sane(clip):
        try:
            length = float(clip.length)
        except Exception:
            return False
        return math.isfinite(length) and 0 < length <= CLIP_LENGTH_MAX

    def _park_recording(self, key, track, scene, slot):
        """Кліп під запис невидимий для дзеркала: воно лишається None.

        Наслідок, який нам і потрібен: якщо запис перервати, has_clip впаде
        назад у None, previous == current, і ClipDelete не піде -- ми ж
        створення не анонсували.
        """
        self._mirror["clips"][key] = None
        self._mirror["notes"].pop(key, None)
        self._note_pending.pop(key, None)
        self._rec_pending[key] = {
            "track": track, "scene": scene, "slot": slot,
            "length": None, "stable_since": None,
        }

    def _flush_recording_clips(self):
        """Завершений запис -> ClipCreate з реальною довжиною.

        Listener'а на clip.length у LOM немає, а прапорець і довжина
        оновлюються не в один момент -- тому чекаємо, доки довжина
        не перестане мінятись. Стелі за часом немає навмисно: залупований
        запис може тривати скільки завгодно, і таймаут означав би віддати
        партнеру заглушку.
        """
        for key in list(self._rec_pending.keys()):
            pending = self._rec_pending[key]
            track, scene, slot = pending["track"], pending["scene"], pending["slot"]
            try:
                if not slot.has_clip:
                    del self._rec_pending[key]
                    continue
                clip = slot.clip
            except Exception:
                del self._rec_pending[key]
                continue

            if self._clip_is_recording(slot, clip):
                pending["stable_since"] = None
                continue

            try:
                length = round(float(clip.length), 6)
            except Exception:
                continue
            now = time.time()
            if pending["length"] != length:
                pending["length"] = length
                pending["stable_since"] = now
                continue
            if now - (pending["stable_since"] or now) < REC_SETTLE_SEC:
                continue

            del self._rec_pending[key]
            if not self._registry_ready or self._suppress_struct:
                continue
            try:
                kind = "midi" if clip.is_midi_clip else "audio"
            except Exception:
                continue
            self._mirror["clips"][key] = kind
            if kind != "midi":
                # Аудіо-кліп ми не створюємо; попередження -- про готовий
                # дубль, а не про намір, тож і місце йому саме тут.
                self._warn("audio clip creation is not synchronized; "
                           "collect the sample and copy the .als structure")
                continue
            payload = self._clip_refs(track, scene)
            payload["clip"] = self._clip_meta(clip)
            self._emit("ClipCreate", payload)
            notes = self._clip_notes(clip)
            self._mirror["notes"][key] = notes
            if notes:
                self._emit_all_note_regions(track, scene, clip, notes)

    def _resolve_clip_slot(self, payload, gseq):
        track, _idx = self._resolve_track(payload.get("track"))
        if track is None:
            return None, None, None
        sidx = self._resolve_scene(payload.get("scene"))
        if sidx is None or sidx >= len(track.clip_slots):
            self._warn("gseq %s: clip scene is outside the track slots" % (gseq,))
            return None, None, None
        return track, self._doc.scenes[sidx], track.clip_slots[sidx]

    def _clip_length_from_payload(self, payload):
        try:
            length = float((payload.get("clip") or {}).get("length", NOTE_TIME_SPAN))
        except Exception:
            length = NOTE_TIME_SPAN
        if not math.isfinite(length) or length <= 0 or length > CLIP_LENGTH_MAX:
            length = NOTE_TIME_SPAN
        return length

    def _ensure_midi_clip(self, track, scene, slot, payload):
        try:
            if slot.has_clip:
                return slot.clip if slot.clip.is_midi_clip else None
        except Exception:
            return None
        key = self._clip_key(track, scene)
        if key is not None:
            self._mirror["clips"][key] = "midi"
            self._mirror["notes"][key] = []
        slot.create_clip(self._clip_length_from_payload(payload))
        clip = slot.clip
        name = (payload.get("clip") or {}).get("name")
        if isinstance(name, str):
            try:
                clip.name = name
            except Exception:
                pass
        color = (payload.get("clip") or {}).get("color")
        if isinstance(color, int) and not isinstance(color, bool) and 0 <= color <= 0xFFFFFF:
            try:
                clip.color = color
            except Exception:
                pass
        return clip if clip.is_midi_clip else None

    def _validated_note_region(self, payload):
        region = payload.get("region") or {}
        try:
            from_pitch = int(region.get("from_pitch"))
            pitch_span = int(region.get("pitch_span"))
            from_time = round(float(region.get("from_time")), 6)
            time_span = round(float(region.get("time_span")), 6)
        except Exception:
            return None
        if (not math.isfinite(from_time) or not math.isfinite(time_span)
                or from_pitch < 0 or pitch_span <= 0 or from_pitch + pitch_span > 128
                or time_span <= 0):
            return None
        notes = payload.get("notes")
        if not isinstance(notes, list) or len(notes) > 4096:
            return None
        normal = []
        for raw in notes:
            note = self._normal_note(raw)
            if note is None:
                return None
            if not (from_pitch <= note["pitch"] < from_pitch + pitch_span
                    and from_time <= note["start_time"] < from_time + time_span):
                return None
            normal.append(note)
        return ((from_pitch, pitch_span, from_time, time_span),
                sorted(normal, key=self._note_signature))

    def _make_note_spec(self, note):
        return Live.Clip.MidiNoteSpecification(
            pitch=note["pitch"],
            start_time=note["start_time"],
            duration=note["duration"],
            velocity=note["velocity"],
            mute=note["mute"],
            probability=note["probability"],
            velocity_deviation=note["velocity_deviation"],
            release_velocity=note["release_velocity"],
        )

    def _apply_note_region(self, track, scene, slot, payload, gseq):
        validated = self._validated_note_region(payload)
        if validated is None:
            self._warn("gseq %s: invalid MIDI note region" % (gseq,))
            return
        region, target = validated
        clip = self._ensure_midi_clip(track, scene, slot, payload)
        if clip is None:
            self._warn("gseq %s: target slot is not a MIDI clip" % (gseq,))
            return

        current_records = self._clip_note_records(clip)
        current_region_records = [
            record for record in current_records
            if self._note_in_region(record[0], region)
        ]
        current_region = sorted([note for note, _note_id in current_region_records],
                                key=self._note_signature)
        if current_region == target:
            self._prime_note_clip(track, scene, slot)
            self._prime_clip_loop(track, scene, slot)
            return

        # Construct every new note before mutating Live. A constructor failure
        # therefore cannot leave a half-removed region.
        specs = tuple(self._make_note_spec(note) for note in target)
        ids = tuple(note_id for _note, note_id in current_region_records if note_id is not None)
        unaffected = [
            note for note, _note_id in current_records
            if not self._note_in_region(note, region)
        ]
        key = self._clip_key(track, scene)
        if key is not None:
            self._mirror["clips"][key] = "midi"
            self._mirror["notes"][key] = sorted(unaffected + target, key=self._note_signature)
        try:
            if ids:
                clip.remove_notes_by_id(ids)
            elif current_region_records:
                clip.remove_notes_extended(region[0], region[1], region[2], region[3])
            if specs:
                clip.add_new_notes(specs)
        except Exception:
            self._prime_note_clip(track, scene, slot)
            self._prime_clip_loop(track, scene, slot)
            raise

    # ------------------------------------------------------------ coalescing

    def _defer(self, key, etype, payload):
        """Р’С–РґРєР»Р°РґР°С” РїРѕРґС–СЋ; РїРѕРІС‚РѕСЂРЅРёР№ РІРёРєР»РёРє Р· С‚РёРј СЃР°РјРёРј РєР»СЋС‡РµРј Р·Р°С‚РёСЂР°С” РїРѕРїРµСЂРµРґРЅСЋ."""
        now = time.time()
        prev = self._pending.get(key)
        self._pending[key] = {
            "type": etype,
            "payload": payload,
            "due": now + DEBOUNCE_SEC,
            "first": prev["first"] if prev else now,
        }

    def _flush_pending(self, force=False):
        now = time.time()
        for key in list(self._pending.keys()):
            e = self._pending[key]
            if force or now >= e["due"] or now - e["first"] >= DEBOUNCE_MAX_HOLD:
                del self._pending[key]
                self._emit(e["type"], e["payload"])

    def _flush_clips(self):
        """Р РѕР·Р±РёСЂР°С” РЅР°РєРѕРїРёС‡РµРЅС– Р·РјС–РЅРё СЃР»РѕС‚С–РІ Сѓ СЃРµРјР°РЅС‚РёС‡РЅС– РїРѕРґС–С—.

        Р—Р°РїСѓСЃРє СЃС†РµРЅРё РІРёРґРЅРѕ СЏРє В«РєС–Р»СЊРєР° С‚СЂРµРєС–РІ РѕРґРЅРѕС‡Р°СЃРЅРѕ РїРѕС—С…Р°Р»Рё РЅР° С‚РѕР№ СЃР°РјРёР№ С–РЅРґРµРєСЃВ»:
        Р·РіРѕСЂС‚Р°С”РјРѕ РІ РѕРґРЅСѓ SceneLaunch. РЎСѓРїСѓС‚РЅС– Р·СѓРїРёРЅРєРё С‚СЂРµРєС–РІ Р±РµР· РєР»С–РїСѓ РІ С†С–Р№ СЃС†РµРЅС–
        РЅРµ РІС–РґРїСЂР°РІР»СЏС”РјРѕ -- scene.fire() РЅР° С‚РѕРјСѓ Р±РѕС†С– РІС–РґС‚РІРѕСЂРёС‚СЊ С—С… СЃР°Рј.
        """
        if not self._clip_buf:
            return
        if not self._registry_ready:
            self._clip_buf = {}
            self._warn("СЂРµС”СЃС‚СЂ С‰Рµ РЅРµ РіРѕС‚РѕРІРёР№ -- Р·РјС–РЅРё РєР»С–РїС–РІ РЅРµ РІС–РґРїСЂР°РІР»РµРЅРѕ")
            return
        buf, self._clip_buf = self._clip_buf, {}

        launched = [i for i, psi in buf.items() if psi >= 0]
        targets = set(buf[i] for i in launched)
        if len(targets) == 1 and len(launched) >= 2:
            self._emit("SceneLaunch", {"scene": self._scene_ref(targets.pop())})
            return

        # Stop All Clips: СѓСЃРµ, С‰Рѕ Р·РјС–РЅРёР»РѕСЃСЊ, Р·СѓРїРёРЅРёР»РѕСЃСЊ, С– РЅС–РґРµ Р±С–Р»СЊС€Рµ РЅС–С‡РѕРіРѕ РЅРµ РіСЂР°С”.
        # Р”СЂСѓРіР° СѓРјРѕРІР° РѕР±РѕРІКјСЏР·РєРѕРІР° -- Р±РµР· РЅРµС— РґРІС– Р·СѓРїРёРЅРєРё РїРѕСЃРїС–Р»СЊ РІ РѕРґРЅРѕРјСѓ С‚С–РєСѓ
        # РІРёРіР»СЏРґР°Р»Рё Р± СЏРє РіР»РѕР±Р°Р»СЊРЅРёР№ СЃС‚РѕРї С– Р·Р°РіР»СѓС€РёР»Рё Р± РїР°СЂС‚РЅРµСЂСѓ СЂРµС€С‚Сѓ С‚СЂРµРєС–РІ.
        if len(launched) == 0 and len(buf) >= 2:
            if not [v for v in self._mirror["psi"].values() if v >= 0]:
                self._emit("StopAllClips", {})
                return

        tracks = self._doc.tracks
        for idx in sorted(buf):
            if idx >= len(tracks):
                continue  # С‚СЂРµРє Р·РЅРёРє РјС–Р¶ С‚С–РєРѕРј С– С„Р»Р°С€РµРј
            ref = self._track_ref(tracks[idx], idx)
            if buf[idx] < 0:
                self._emit("ClipStop", {"track": ref})
            else:
                self._emit("ClipLaunch", {"track": ref, "scene": self._scene_ref(buf[idx])})

    # ----------------------------------------------------------------- apply

    def _apply(self, etype, payload, gseq):
        self._log("<- #%s %s %r" % (gseq, etype, payload))

        if etype == "TransportSet":
            want = bool(payload.get("playing"))
            self._mirror["playing"] = want  # Р”Рћ Р·Р°РїРёСЃСѓ РІ LOM -- РіР»СѓС€РёРјРѕ РµС…Рѕ
            if want:
                self._doc.start_playing()
            else:
                self._doc.stop_playing()

        elif etype == "TempoSet":
            bpm = float(payload.get("bpm"))
            self._mirror["tempo"] = round(bpm, 6)
            self._doc.tempo = bpm

        elif etype == "ClipLaunch":
            track, idx = self._resolve_track(payload.get("track"))
            if track is None or idx is None:
                return
            sidx = self._resolve_scene(payload.get("scene"))
            if sidx is None or sidx >= len(track.clip_slots):
                self._warn("gseq %s: СЃС†РµРЅР° %r РїРѕР·Р° РјРµР¶Р°РјРё" % (gseq, payload.get("scene")))
                return
            self._mirror["psi"][idx] = sidx
            track.clip_slots[sidx].fire()

        elif etype == "SceneLaunch":
            sidx = self._resolve_scene(payload.get("scene"))
            if sidx is None:
                self._warn("gseq %s: СЃС†РµРЅР° %r РЅРµ СЂРµР·РѕР»РІРёС‚СЊСЃСЏ" % (gseq, payload.get("scene")))
                return
            # РґР·РµСЂРєР°Р»Рѕ С‚СЂРµР±Р° Р·РІРµСЃС‚Рё РґРѕ С‚РѕРіРѕ, С‰Рѕ СЃС‚Р°РЅРµС‚СЊСЃСЏ РџР†РЎР›РЇ fire(): С‚СЂРµРєРё Р· РєР»С–РїРѕРј
            # Сѓ С†С–Р№ СЃС†РµРЅС– Р·Р°РіСЂР°СЋС‚СЊ С—С—, СЂРµС€С‚Р° Р·СѓРїРёРЅСЏС‚СЊСЃСЏ -- С–РЅР°РєС€Рµ РїС–РґРµ РµС…Рѕ
            for i, t in enumerate(self._doc.tracks):
                try:
                    has_clip = sidx < len(t.clip_slots) and t.clip_slots[sidx].has_clip
                except Exception:
                    has_clip = False
                self._mirror["psi"][i] = sidx if has_clip else -1
            self._doc.scenes[sidx].fire()

        elif etype == "TrackCreate":
            ref = payload.get("track") or {}
            uid = ref.get("id")
            if not uid or self._tracks_reg.obj_of(uid) is not None:
                return  # С‚Р°РєРёР№ С‚СЂРµРє СѓР¶Рµ С” -- РїРѕРІС‚РѕСЂРЅРµ Р·Р°СЃС‚РѕСЃСѓРІР°РЅРЅСЏ РЅРµ СЃС‚РІРѕСЂСЋС” РґСѓР±Р»СЊ
            idx = payload.get("idx")
            if not isinstance(idx, int) or idx < 0 or idx > len(self._doc.tracks):
                idx = len(self._doc.tracks)
            kind = payload.get("kind")
            if kind not in ("midi", "audio"):
                # Невідомий різновид не приводимо до відомого: група, що
                # приїхала як audio, дала б фантомний порожній трек.
                self._warn("gseq %s: невідомий різновид треку %r, подію пропущено"
                           % (gseq, kind))
                return
            self._suppress_struct = True
            try:
                if kind == "midi":
                    self._doc.create_midi_track(idx)
                else:
                    self._doc.create_audio_track(idx)
                new = self._doc.tracks[idx]
                if isinstance(ref.get("name"), str):
                    new.name = ref["name"]
                color = ref.get("color")
                if isinstance(color, int) and not isinstance(color, bool) and 0 <= color <= 0xFFFFFF:
                    new.color = color
                self._tracks_reg.bind(uid, new)
            finally:
                self._suppress_struct = False
                self._diff_tracks(emit=False)

        elif etype == "TrackDelete":
            uid = (payload.get("track") or {}).get("id")
            track = self._tracks_reg.obj_of(uid) if uid else None
            if track is None:
                return  # tombstone: РѕР±'С”РєС‚Р° РІР¶Рµ РЅРµРјР°С”, РґС–СЏ РІ РїРѕСЂРѕР¶РЅРµС‡Сѓ РЅРµ Р№РґРµ
            idx = self._track_index(track)
            if idx is None:
                return
            self._suppress_struct = True
            try:
                self._doc.delete_track(idx)
                self._tracks_reg.forget(uid)
            finally:
                self._suppress_struct = False
                self._diff_tracks(emit=False)

        elif etype == "SceneCreate":
            ref = payload.get("scene") or {}
            uid = ref.get("id")
            if not uid or self._scenes_reg.obj_of(uid) is not None:
                return
            idx = payload.get("idx")
            if not isinstance(idx, int) or idx < 0 or idx > len(self._doc.scenes):
                idx = len(self._doc.scenes)
            self._suppress_struct = True
            try:
                self._doc.create_scene(idx)
                new = self._doc.scenes[idx]
                if isinstance(ref.get("name"), str):
                    new.name = ref["name"]
                color = ref.get("color")
                if isinstance(color, int) and not isinstance(color, bool) and 0 <= color <= 0xFFFFFF:
                    new.color = color
                self._scenes_reg.bind(uid, new)
            finally:
                self._suppress_struct = False
                self._diff_scenes(emit=False)

        elif etype == "SceneDelete":
            uid = (payload.get("scene") or {}).get("id")
            scene = self._scenes_reg.obj_of(uid) if uid else None
            if scene is None:
                return
            scenes = self._doc.scenes
            idx = None
            for i in range(len(scenes)):
                if scenes[i] == scene:
                    idx = i
                    break
            if idx is None:
                return
            self._suppress_struct = True
            try:
                self._doc.delete_scene(idx)
                self._scenes_reg.forget(uid)
            finally:
                self._suppress_struct = False
                self._diff_scenes(emit=False)

        elif etype == "ObjectMetaSet":
            kind = payload.get("object")
            prop = payload.get("prop")
            if prop not in ("name", "color"):
                self._warn("gseq %s: metadata property %r is unknown" % (gseq, prop))
                return
            target = None
            track = None
            scene = None
            if kind == "track":
                target, _track_ref = self._resolve_device_track(payload.get("track"))
                track = target
            elif kind == "scene":
                sidx = self._resolve_scene(payload.get("scene"))
                if sidx is not None:
                    target = scene = self._doc.scenes[sidx]
            elif kind == "clip":
                track, scene, slot = self._resolve_clip_slot(payload, gseq)
                try:
                    target = slot.clip if slot is not None and slot.has_clip else None
                except Exception:
                    target = None
            else:
                self._warn("gseq %s: metadata object %r is unknown" % (gseq, kind))
                return
            if target is None:
                self._warn("gseq %s: metadata target is absent" % (gseq,))
                return
            value = payload.get("value")
            if prop == "name":
                if not isinstance(value, str):
                    self._warn("gseq %s: metadata name is not a string" % (gseq,))
                    return
            elif not (isinstance(value, int) and not isinstance(value, bool)
                      and 0 <= value <= 0xFFFFFF):
                self._warn("gseq %s: metadata color is outside RGB range" % (gseq,))
                return
            address = self._metadata_address(kind, target, track, scene)
            if address is None:
                return
            key = self._metadata_key(address, prop)
            self._mirror["meta"][key] = value
            try:
                setattr(target, prop, value)
                actual = self._metadata_value(target, prop)
                if actual is not None:
                    self._mirror["meta"][key] = actual
            except Exception as e:
                self._warn("gseq %s: metadata %s could not be set: %r" % (gseq, prop, e))
                return
            if prop == "name" and kind in ("track", "scene"):
                if kind == "track" and self._aux_kind_of(track):
                    self._refresh_aux_tracks()
                self._persist_registry()

        elif etype == "ClipCreate":
            track, scene, slot = self._resolve_clip_slot(payload, gseq)
            if slot is None:
                return
            try:
                if slot.has_clip and not slot.clip.is_midi_clip:
                    self._warn("gseq %s: cannot replace an audio clip with a MIDI clip" % (gseq,))
                    return
            except Exception:
                return
            self._suppress_struct = True
            try:
                clip = self._ensure_midi_clip(track, scene, slot, payload)
                if clip is None:
                    self._warn("gseq %s: MIDI clip could not be created" % (gseq,))
            finally:
                self._suppress_struct = False
                self._rewire_tracks()
                self._prime_note_clip(track, scene, slot)
                self._prime_clip_loop(track, scene, slot)
                self._prime_metadata()
                self._prime_clip_loops()

        elif etype == "ClipDelete":
            track, scene, slot = self._resolve_clip_slot(payload, gseq)
            if slot is None:
                return
            key = self._clip_key(track, scene)
            if key is not None:
                self._mirror["clips"][key] = None
                self._mirror["notes"].pop(key, None)
                self._note_pending.pop(key, None)
            self._suppress_struct = True
            try:
                if slot.has_clip:
                    slot.delete_clip()
            finally:
                self._suppress_struct = False
                self._rewire_tracks()
                self._prime_note_clip(track, scene, slot)
                self._prime_clip_loop(track, scene, slot)
                self._prime_metadata()
                self._prime_clip_loops()

        elif etype == "ClipLoopSet":
            track, scene, slot = self._resolve_clip_slot(payload, gseq)
            if slot is None:
                return
            try:
                if not slot.has_clip:
                    self._warn("gseq %s: кліпу немає, межі нема на що класти" % (gseq,))
                    return
                clip = slot.clip
            except Exception:
                return

            # Спершу повна валідація, і лише потім записи: частковий запис
            # у LOM гірший за відмову.
            state = {}
            for prop in CLIP_LOOP_PROPS:
                if prop not in payload:
                    continue
                value = payload[prop]
                if prop == "looping":
                    if not isinstance(value, bool):
                        return self._warn("gseq %s: looping має бути булевим" % (gseq,))
                    state[prop] = value
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    return self._warn("gseq %s: %s має бути числом" % (gseq, prop))
                value = float(value)
                if not math.isfinite(value) or abs(value) > CLIP_LENGTH_MAX:
                    return self._warn("gseq %s: %s поза межами" % (gseq, prop))
                state[prop] = value
            if not state:
                return
            for lo, hi in (("loop_start", "loop_end"), ("start_marker", "end_marker")):
                if lo in state and hi in state and state[hi] <= state[lo]:
                    return self._warn("gseq %s: %s не більший за %s" % (gseq, hi, lo))

            key = self._clip_key(track, scene)
            if key is not None:
                self._mirror["loop"][key] = dict(state)
            # Порядок незвертальний: якщо нова пара цілком правіша за поточну,
            # спершу треба посунути кінець, інакше Live клампне початок.
            for lo, hi in (("start_marker", "end_marker"), ("loop_start", "loop_end")):
                pair = [p for p in (lo, hi) if p in state]
                if len(pair) == 2:
                    try:
                        if state[lo] >= float(getattr(clip, hi)):
                            pair = [hi, lo]
                    except Exception:
                        pass
                for prop in pair:
                    try:
                        setattr(clip, prop, state[prop])
                    except Exception:
                        self._warn("gseq %s: %s не записався" % (gseq, prop))
            if "looping" in state:
                try:
                    clip.looping = state["looping"]
                except Exception:
                    self._warn("gseq %s: looping не записався" % (gseq,))
            # Live міг клампнути -- дзеркало має відповідати тому, що вийшло
            if key is not None:
                actual = self._clip_loop_state(clip)
                if actual is not None:
                    self._mirror["loop"][key] = actual

        elif etype == "ClipNotesSet":
            track, scene, slot = self._resolve_clip_slot(payload, gseq)
            if slot is None:
                return
            self._apply_note_region(track, scene, slot, payload, gseq)

        elif etype == "MixerSet":
            track, track_ref = self._resolve_device_track(payload.get("track"))
            if track is None:
                return
            param = payload.get("param")
            idx = payload.get("index")
            p = self._mix_param(track, param, idx)
            if p is None:
                self._warn("gseq %s: РїР°СЂР°РјРµС‚СЂ %r/%r РІС–РґСЃСѓС‚РЅС–Р№" % (gseq, param, idx))
                return
            try:
                value = float(payload.get("value"))
            except Exception:
                return
            # DeviceParameter РєРёРґР°С” РїСЂРё РІРёС…РѕРґС– Р·Р° РјРµР¶С–, Р° РјРµР¶С– send-С–РІ
            # РІС–РґСЂС–Р·РЅСЏСЋС‚СЊСЃСЏ РІС–Рґ volume -- Р±РµСЂРµРјРѕ С—С… Р· СЃР°РјРѕРіРѕ РїР°СЂР°РјРµС‚СЂР°
            value = max(p.min, min(p.max, value))
            self._mirror["mix"][self._mix_key(track_ref, param, idx)] = round(value, 6)
            p.value = value

        elif etype == "DeviceParamSet":
            track, track_ref = self._resolve_device_track(payload.get("track"))
            if track is None:
                return
            device_ref = payload.get("device")
            parameter_ref = payload.get("parameter")
            chain_path = payload.get("chain_path") or []
            device, parameter = self._resolve_device_parameter(
                track, chain_path, device_ref, parameter_ref)
            if device is None:
                self._warn("gseq %s: device %r at chain path %r is absent; parameter event skipped"
                           % (gseq, device_ref, chain_path))
                return
            if parameter is None:
                self._warn("gseq %s: parameter %r is absent on device %r; event skipped"
                           % (gseq, parameter_ref, device_ref))
                return
            try:
                value = float(payload.get("value"))
            except Exception:
                return
            if math.isnan(value) or math.isinf(value):
                self._warn("gseq %s: non-finite device parameter value" % (gseq,))
                return
            try:
                if not bool(parameter.is_enabled):
                    self._warn("gseq %s: parameter %r is disabled; event skipped"
                               % (gseq, parameter_ref))
                    return
            except Exception:
                pass
            value = max(float(parameter.min), min(float(parameter.max), value))
            key = self._device_key(track_ref, chain_path, device_ref, parameter_ref)
            self._mirror["device"][key] = round(value, 6)
            try:
                parameter.value = value
            except Exception as e:
                self._warn("gseq %s: device parameter could not be set: %r" % (gseq, e))

        elif etype == "TrackToggle":
            track, track_ref = self._resolve_device_track(payload.get("track"))
            if track is None:
                return
            prop = payload.get("param")
            if prop not in self._toggle_props(track):
                self._warn("gseq %s: РЅРµРІС–РґРѕРјРёР№ РїРµСЂРµРјРёРєР°С‡ %r" % (gseq, prop))
                return
            value = bool(payload.get("value"))
            self._mirror["mix"][self._toggle_key(track_ref, prop)] = value
            try:
                setattr(track, prop, value)
            except Exception as e:
                self._warn("gseq %s: %s РЅРµ РІСЃС‚Р°РЅРѕРІР»СЋС”С‚СЊСЃСЏ: %r" % (gseq, prop, e))

        elif etype == "StopAllClips":
            for i in range(len(self._doc.tracks)):
                self._mirror["psi"][i] = -1
            self._doc.stop_all_clips()

        elif etype == "ClipStop":
            track, idx = self._resolve_track(payload.get("track"))
            if track is None or idx is None:
                return
            self._mirror["psi"][idx] = -1
            track.stop_all_clips()

        else:
            self._warn("РЅРµРІС–РґРѕРјРёР№ С‚РёРї РїРѕРґС–С— %r (gseq %s)" % (etype, gseq))

    # --------------------------------------------------------------- pumping

    def update_display(self):
        """Live РєР»РёС‡Рµ С†Рµ ~10 СЂР°Р·С–РІ РЅР° СЃРµРєСѓРЅРґСѓ -- РЅР°С€ С”РґРёРЅРёР№ РЅР°РґС–Р№РЅРёР№ tick."""
        try:
            ControlSurface.update_display(self)
        except Exception:
            self._log_exc("base update_display")
        self._safe(self._pump)

    def _pump(self):
        if self._chat is not None:
            self._chat.poll(self._handle_chat_request)
        if self._link is None or not self._link.alive:
            return
        for msg in self._link.poll():
            self._safe(self._dispatch, msg)
        self._safe(self._flush_clips)
        self._safe(self._flush_notes)
        self._safe(self._flush_recording_clips)
        self._safe(self._flush_pending)
        self._safe(self._flush_view)
        self._safe(self._flush_state)
        self._safe(self._flush_state_apply)
        now = time.time()
        if now - self._last_beat >= HEARTBEAT_SEC:
            self._last_beat = now
            self._link.send({"m": "heartbeat", "t": now})

    def _dispatch(self, msg):
        m = msg.get("m")
        if m == "apply":
            gseq = msg.get("gseq")
            try:
                self._apply(msg.get("type"), msg.get("payload") or {}, gseq)
            except Exception as e:
                self._link.send({"m": "apply_ack", "gseq": gseq,
                                 "ok": False, "error": repr(e)})
                raise
            self._link.send({"m": "apply_ack", "gseq": gseq, "ok": True})
        elif m == "state_apply":
            self._safe(self._start_state_apply, msg.get("path"), msg.get("id"))
        elif m == "view_set":
            self._safe(self._apply_view, msg)
        elif m == "view_request":
            self._safe(self._touch_view, True)
        elif m == "state_request":
            self._safe(self._queue_state, msg.get("id"))
        elif m == "snapshot_request":
            self._link.send({"m": "snapshot", "state": self._snapshot()})
        elif m == "hello_request":
            # daemon стартував пізніше за Live і пропустив наш hello
            self._link.send({
                "m": "hello",
                "live": self._live_version(),
                "script": SCRIPT_VERSION,
                "pid": os.getpid(),
                "events": APPLY_TYPES,
                "features": FEATURES,
            })
        elif m == "registry_build":
            self._link.send({"m": "registry", "registry": self._build_registry()})
        elif m == "registry_adopt":
            self._adopt_registry(msg.get("registry") or {})
        elif m == "ping":
            self._link.send({"m": "heartbeat", "t": time.time()})

    # ------------------------------------------------------------ addressing

    def _track_index(self, track):
        try:
            tracks = self._doc.tracks
            for i in range(len(tracks)):
                if tracks[i] == track:
                    return i
        except Exception:
            pass
        return None

    def _track_ref(self, track, idx=None):
        """РђРґСЂРµСЃР° -- uuid. idx С– name Р»РёС€Р°СЋС‚СЊСЃСЏ С‚С–Р»СЊРєРё РґР»СЏ С‡РёС‚Р°Р±РµР»СЊРЅРѕСЃС‚С– Р»РѕРіС–РІ."""
        return {"id": self._tracks_reg.id_of(track), "name": self._safe_name(track)}

    def _scene_ref(self, idx):
        scenes = self._doc.scenes
        if idx < 0 or idx >= len(scenes):
            return {"id": None}
        scene = scenes[idx]
        ref = {"id": self._scenes_reg.id_of(scene)}
        # РЈ СЃС†РµРЅ Live Р·Р° Р·Р°РјРѕРІС‡СѓРІР°РЅРЅСЏРј С–РјРµРЅС– РЅРµРјР°С” (С†РёС„СЂРё РІ UI -- С†Рµ С–РЅРґРµРєСЃРё,
        # РЅРµ РЅР°Р·РІРё), С‚РѕР¶ РїРѕСЂРѕР¶РЅС” РїРѕР»Рµ РЅРµ РєР»Р°РґРµРјРѕ РІР·Р°РіР°Р»С–.
        name = self._safe_name(scene)
        if name:
            ref["name"] = name
        return ref

    def _resolve_track(self, ref):
        """РџРѕРІРµСЂС‚Р°С” (РѕР±'С”РєС‚, РїРѕС‚РѕС‡РЅРёР№ С–РЅРґРµРєСЃ) Р°Р±Рѕ (None, None)."""
        if not isinstance(ref, dict):
            return None, None
        uid = ref.get("id")
        track = self._tracks_reg.obj_of(uid) if uid else None
        if track is None:
            # РЅРµРІС–РґРѕРјРёР№ uuid = РѕР±'С”РєС‚Р° С‚СѓС‚ РЅРµРјР°С” Р°Р±Рѕ РІС–РЅ РІРёРґР°Р»РµРЅРёР№ (tombstone)
            self._warn("С‚СЂРµРє %r РЅРµРІС–РґРѕРјРёР№, РїРѕРґС–СЋ РїСЂРѕРїСѓС‰РµРЅРѕ" % (uid,))
            return None, None
        return track, self._track_index(track)

    def _resolve_device_track(self, ref):
        """Resolve a DeviceParamSet container without widening ordinary track APIs."""
        if not isinstance(ref, dict):
            return None, None
        kind = ref.get("kind")
        if kind is None:
            track, _idx = self._resolve_track(ref)
            return (track, {"id": ref.get("id")}) if track is not None else (None, None)
        if kind not in ("return", "master"):
            self._warn("device track kind %r is unknown; event skipped" % (kind,))
            return None, None
        uid = ref.get("id")
        track = self._aux_tracks_reg.obj_of(uid) if uid else None
        if track is None or self._aux_kind_of(track) != kind:
            self._warn("%s device track %r is unknown; event skipped" % (kind, uid))
            return None, None
        return track, {"id": uid, "kind": kind}

    def _resolve_scene(self, ref):
        """РЎС†РµРЅСѓ Р°РґСЂРµСЃСѓС”РјРѕ uuid, Р°Р»Рµ LOM РїСЂР°С†СЋС” С–РЅРґРµРєСЃР°РјРё -- РІРµСЂС‚Р°С”РјРѕ С–РЅРґРµРєСЃ."""
        if not isinstance(ref, dict):
            return None
        uid = ref.get("id")
        scene = self._scenes_reg.obj_of(uid) if uid else None
        if scene is None:
            self._warn("СЃС†РµРЅР° %r РЅРµРІС–РґРѕРјР°, РїРѕРґС–СЋ РїСЂРѕРїСѓС‰РµРЅРѕ" % (uid,))
            return None
        scenes = self._doc.scenes
        for i in range(len(scenes)):
            if scenes[i] == scene:
                return i
        return None

    def _safe_name(self, obj):
        try:
            return str(obj.name)
        except Exception:
            return ""

    # --------------------------------------------------------------- samples

    def _iter_audio_clips(self):
        for track in self._doc.tracks:
            try:
                slots = list(track.clip_slots)
            except Exception:
                slots = []
            for slot in slots:
                try:
                    if slot.has_clip and not slot.clip.is_midi_clip:
                        yield slot.clip
                except Exception:
                    pass
            try:
                for clip in track.arrangement_clips:
                    if not clip.is_midi_clip:
                        yield clip
            except Exception:
                pass

    def _scan_samples(self):
        """РЇРєС– СЃРµРјРїР»Рё Р»РµР¶Р°С‚СЊ РїРѕР·Р° С‚РµРєРѕСЋ РїСЂРѕС”РєС‚Сѓ С– СЏРєРёС… Р±СЂР°РєСѓС” Р»РѕРєР°Р»СЊРЅРѕ.

        Live Р·Р° Р·Р°РјРѕРІС‡СѓРІР°РЅРЅСЏРј РЅРµ РєРѕРїС–СЋС” СЃРµРјРїР» Сѓ РїСЂРѕС”РєС‚ -- .als С‚СЂРёРјР°С” РїРѕСЃРёР»Р°РЅРЅСЏ
        РЅР° РѕСЂРёРіС–РЅР°Р». РўР°РєРёР№ РїСЂРѕС”РєС‚ РЅРµРїРµСЂРµРЅРѕСЃРёРјРёР№: Сѓ РїР°СЂС‚РЅРµСЂР° Р°Р±СЃРѕР»СЋС‚РЅРёР№ С€Р»СЏС…
        РЅРµ С–СЃРЅСѓС”, С– Live РїРѕРєР°Р¶Рµ missing media. Collect All and Save С‡РµСЂРµР· LOM
        РЅРµ РІРёРєР»РёРєР°С”С‚СЊСЃСЏ, Р° clip.file_path РґРѕСЃС‚СѓРїРЅРёР№ Р»РёС€Рµ РЅР° С‡РёС‚Р°РЅРЅСЏ, С‚РѕР¶
        РІРёРїСЂР°РІРёС‚Рё С†Рµ РєРѕРґРѕРј РЅРµ РјРѕР¶РЅР° -- Р»РёС€Рµ РІС‡Р°СЃРЅРѕ СЃРєР°Р·Р°С‚Рё.
        """
        try:
            root = os.path.dirname(str(self._doc.file_path))
        except Exception:
            root = ""
        root_l = os.path.normcase(os.path.abspath(root)) if root else ""

        total = 0
        external = []
        missing = []
        for clip in self._iter_audio_clips():
            try:
                path = str(clip.file_path)
            except Exception:
                continue
            if not path:
                continue
            total += 1
            if not os.path.exists(path):
                if len(missing) < 20:
                    missing.append(path)
                continue
            if root_l and not os.path.normcase(os.path.abspath(path)).startswith(root_l):
                if len(external) < 20:
                    external.append(path)

        return {
            "total": total,
            "project_root": root,
            "external": external,
            "missing": missing,
        }

    # ---------------------------------------------------------- AI chat LOM

    def _handle_chat_request(self, command, payload):
        if command == "snapshot":
            return self._ai_snapshot()
        if command == "exec":
            return self._ai_exec(payload or {})
        raise ValueError("unknown chat command %r" % (command,))

    def _ai_snapshot(self):
        state = self._snapshot() or {}
        state["script"] = SCRIPT_VERSION
        state["registry_ready"] = bool(self._registry_ready)
        state["tracks"] = []
        for idx, track in enumerate(self._doc.tracks):
            summary = self._ai_track_summary(track, idx=idx, kind=self._track_kind(track))
            try:
                summary["playing_slot_index"] = self._norm_psi(track.playing_slot_index)
            except Exception:
                pass
            summary["clips"] = self._ai_clip_summaries(track)
            state["tracks"].append(summary)
        state["scenes"] = []
        for idx, scene in enumerate(self._doc.scenes):
            item = {"index": idx, "name": self._safe_name(scene)}
            uid = self._scenes_reg.id_of(scene, create=False)
            if uid:
                item["id"] = uid
            color = self._safe_color(scene)
            if color is not None:
                item["color"] = color
            state["scenes"].append(item)
        state["aux_tracks"] = []
        for kind, idx, track in self._iter_aux_tracks():
            summary = self._ai_track_summary(track, idx=idx, kind=kind)
            summary["aux_kind"] = kind
            state["aux_tracks"].append(summary)
        return state

    def _ai_track_summary(self, track, idx=None, kind=None):
        ref = self._device_track_ref(track)
        item = {
            "name": self._safe_name(track),
            "kind": kind or self._track_kind(track),
            "mixer": {},
            "toggles": {},
            "devices": [],
        }
        if idx is not None:
            item["index"] = idx
        if ref and ref.get("id"):
            item["id"] = ref["id"]
        color = self._safe_color(track)
        if color is not None:
            item["color"] = color
        for param, send_idx in self._mix_slots(track):
            p = self._mix_param(track, param, send_idx)
            if p is None:
                continue
            key = param if send_idx is None else "%s:%d" % (param, send_idx)
            item["mixer"][key] = self._ai_parameter_summary(p)
        for prop in self._toggle_props(track):
            try:
                item["toggles"][prop] = bool(getattr(track, prop))
            except Exception:
                pass
        device_count = 0
        for container, device, chain_path in self._iter_track_devices(track):
            if device_count >= 64:
                item["devices_truncated"] = True
                break
            device_count += 1
            device_ref = self._device_ref(container, device)
            if device_ref is None:
                continue
            d = dict(device_ref)
            d["name"] = self._safe_name(device)
            if chain_path:
                d["chain_path"] = chain_path
            params = []
            try:
                parameters = list(device.parameters)
            except Exception:
                parameters = []
            for pidx, parameter in enumerate(parameters[:128]):
                pref = self._device_parameter_ref(device, parameter) or {"index": pidx}
                ps = dict(pref)
                ps.update(self._ai_parameter_summary(parameter))
                params.append(ps)
            if len(parameters) > 128:
                d["parameters_truncated"] = True
            d["parameters"] = params
            item["devices"].append(d)
        return item

    def _ai_parameter_summary(self, parameter):
        out = {}
        try:
            out["name"] = str(parameter.name)
        except Exception:
            pass
        try:
            out["value"] = round(float(parameter.value), 6)
        except Exception:
            pass
        for attr in ("min", "max"):
            try:
                out[attr] = round(float(getattr(parameter, attr)), 6)
            except Exception:
                pass
        try:
            out["is_enabled"] = bool(parameter.is_enabled)
        except Exception:
            pass
        try:
            out["is_quantized"] = bool(parameter.is_quantized)
        except Exception:
            pass
        return out

    def _ai_clip_summaries(self, track):
        clips = []
        try:
            slots = list(track.clip_slots)
        except Exception:
            return clips
        scenes = list(self._doc.scenes)
        for scene_idx, slot in enumerate(slots[:len(scenes)]):
            try:
                if not slot.has_clip:
                    continue
                clip = slot.clip
                item = {
                    "scene_index": scene_idx,
                    "kind": "midi" if clip.is_midi_clip else "audio",
                    "name": self._safe_name(clip),
                }
                try:
                    item["length"] = round(float(clip.length), 6)
                except Exception:
                    pass
                color = self._safe_color(clip)
                if color is not None:
                    item["color"] = color
                if clip.is_midi_clip:
                    try:
                        item["notes"] = len(self._clip_notes(clip))
                    except Exception:
                        pass
                clips.append(item)
            except Exception:
                pass
        return clips

    def _ai_exec(self, payload):
        actions = payload.get("actions")
        if not isinstance(actions, list):
            raise ValueError("actions must be a list")
        if len(actions) > 128:
            raise ValueError("too many actions")
        results = []
        ok = True
        for idx, action in enumerate(actions):
            if not isinstance(action, dict):
                ok = False
                results.append({"index": idx, "ok": False, "error": "action must be an object"})
                if not payload.get("continue_on_error"):
                    break
                continue
            op = action.get("op")
            try:
                result = self._ai_exec_action(action)
                results.append({"index": idx, "ok": True, "op": op, "result": result})
            except Exception as e:
                ok = False
                results.append({"index": idx, "ok": False, "op": op, "error": repr(e)})
                self._warn("AI action %r failed: %r" % (op, e))
                if not payload.get("continue_on_error"):
                    break
        return {"ok": ok, "results": results, "snapshot": self._ai_snapshot()}

    def _ai_exec_action(self, action):
        op = action.get("op")
        if not isinstance(op, str):
            raise ValueError("op is required")

        if op == "snapshot":
            return self._ai_snapshot()

        if op == "apply":
            etype = action.get("type")
            payload = action.get("payload") or {}
            if not isinstance(etype, str) or not isinstance(payload, dict):
                raise ValueError("apply requires type and payload")
            self._apply(etype, payload, self._next_ai_seq())
            return {"applied": etype}

        if op == "transport":
            if "playing" in action:
                if bool(action.get("playing")):
                    self._doc.start_playing()
                else:
                    self._doc.stop_playing()
            if "bpm" in action or "tempo" in action:
                bpm = self._ai_float(action.get("bpm", action.get("tempo")), "bpm")
                self._doc.tempo = bpm
            return {"playing": bool(self._doc.is_playing), "tempo": float(self._doc.tempo)}

        if op == "set_tempo":
            bpm = self._ai_float(action.get("bpm", action.get("tempo")), "bpm")
            self._doc.tempo = bpm
            return {"tempo": float(self._doc.tempo)}

        if op == "create_track":
            idx = self._ai_insert_index(action.get("index", action.get("idx")),
                                        len(self._doc.tracks))
            kind = action.get("kind", "audio")
            if kind == "midi":
                self._doc.create_midi_track(idx)
            elif kind == "audio":
                self._doc.create_audio_track(idx)
            else:
                raise ValueError("track kind must be audio or midi")
            track = self._doc.tracks[idx]
            self._ai_set_optional_name_color(track, action)
            return self._ai_serialize(track)

        if op == "delete_track":
            track, idx = self._ai_target_track(action)
            if not isinstance(idx, int):
                raise ValueError("only ordinary tracks can be deleted")
            self._doc.delete_track(idx)
            return {"deleted_track_index": idx}

        if op == "rename_track":
            track, _idx = self._ai_target_track(action)
            name = action.get("name")
            if not isinstance(name, str):
                raise ValueError("name must be a string")
            track.name = name
            return self._ai_serialize(track)

        if op == "set_track_color":
            track, _idx = self._ai_target_track(action)
            track.color = self._ai_color(action.get("color"))
            return self._ai_serialize(track)

        if op == "set_track_toggle":
            track, _idx = self._ai_target_track(action)
            prop = action.get("param", action.get("property"))
            if prop not in self._toggle_props(track):
                raise ValueError("unknown or unsupported track toggle %r" % (prop,))
            setattr(track, prop, bool(action.get("value")))
            return {prop: bool(getattr(track, prop))}

        if op == "set_mixer":
            track, _idx = self._ai_target_track(action)
            param = action.get("param")
            send_idx = action.get("send_index", action.get("index"))
            if param != "send":
                send_idx = None
            else:
                send_idx = self._ai_int(send_idx, "send index")
            p = self._mix_param(track, param, send_idx)
            if p is None:
                raise ValueError("mixer parameter not found")
            value = self._ai_float(action.get("value"), "value")
            p.value = max(float(p.min), min(float(p.max), value))
            return self._ai_parameter_summary(p)

        if op == "create_scene":
            idx = self._ai_insert_index(action.get("index", action.get("idx")),
                                        len(self._doc.scenes))
            self._doc.create_scene(idx)
            scene = self._doc.scenes[idx]
            self._ai_set_optional_name_color(scene, action)
            return self._ai_serialize(scene)

        if op == "delete_scene":
            scene, idx = self._ai_target_scene(action)
            self._doc.delete_scene(idx)
            return {"deleted_scene_index": idx, "name": self._safe_name(scene)}

        if op == "rename_scene":
            scene, _idx = self._ai_target_scene(action)
            name = action.get("name")
            if not isinstance(name, str):
                raise ValueError("name must be a string")
            scene.name = name
            return self._ai_serialize(scene)

        if op == "launch_scene":
            scene, idx = self._ai_target_scene(action)
            scene.fire()
            return {"launched_scene_index": idx}

        if op == "launch_clip":
            _track, _scene, slot = self._ai_target_clip_slot(action)
            slot.fire()
            return {"launched": True}

        if op == "stop_clip":
            track, idx = self._ai_target_track(action)
            track.stop_all_clips()
            return {"stopped_track_index": idx}

        if op == "stop_all_clips":
            self._doc.stop_all_clips()
            return {"stopped": True}

        if op == "create_midi_clip":
            track, scene, slot = self._ai_target_clip_slot(action)
            try:
                if slot.has_clip and not slot.clip.is_midi_clip:
                    raise ValueError("target slot contains an audio clip")
            except AttributeError:
                pass
            length = self._ai_clip_length(action)
            if not slot.has_clip:
                slot.create_clip(length)
            clip = slot.clip
            if not clip.is_midi_clip:
                raise ValueError("target slot is not a MIDI clip")
            self._ai_set_optional_name_color(clip, action)
            return self._ai_serialize(clip)

        if op == "delete_clip":
            _track, _scene, slot = self._ai_target_clip_slot(action)
            if slot.has_clip:
                slot.delete_clip()
            return {"deleted": True}

        if op == "replace_clip_notes":
            return self._ai_replace_clip_notes(action)

        if op == "set_device_parameter":
            return self._ai_set_device_parameter(action)

        if op == "lom_get":
            return self._ai_serialize(self._ai_resolve_path(action.get("path")))

        if op == "lom_set":
            obj = self._ai_resolve_path(action.get("path"))
            prop = action.get("property", action.get("prop"))
            if not isinstance(prop, str) or not prop or prop.startswith("_"):
                raise ValueError("property must be a public string")
            setattr(obj, prop, action.get("value"))
            return self._ai_serialize(obj)

        if op == "lom_call":
            obj = self._ai_resolve_path(action.get("path"))
            method_name = action.get("method")
            if not isinstance(method_name, str) or not method_name or method_name.startswith("_"):
                raise ValueError("method must be a public string")
            method = getattr(obj, method_name)
            args = action.get("args") or []
            kwargs = action.get("kwargs") or {}
            if not isinstance(args, list) or not isinstance(kwargs, dict):
                raise ValueError("args must be a list and kwargs must be an object")
            for key in kwargs:
                if not isinstance(key, str) or key.startswith("_"):
                    raise ValueError("kwargs keys must be public strings")
            return self._ai_serialize(method(*args, **kwargs))

        raise ValueError("unknown op %r" % (op,))

    def _next_ai_seq(self):
        self._ai_seq += 1
        return "ai-%d" % self._ai_seq

    def _ai_target_track(self, action):
        ref = action.get("track")
        if isinstance(ref, dict):
            if "index" in ref:
                return self._track_by_index(ref.get("index"))
            if ref.get("kind") in ("return", "master"):
                track, _track_ref = self._resolve_device_track(ref)
                if track is None:
                    raise ValueError("track target not found")
                return track, None
            if ref.get("id"):
                return self._resolve_track(ref)
        for key in ("track_index", "track_idx"):
            if key in action:
                return self._track_by_index(action.get(key))
        if "track_id" in action:
            return self._resolve_track({"id": action.get("track_id")})
        kind = action.get("track_kind", action.get("aux_kind"))
        if kind in ("return", "master"):
            idx = self._ai_int(action.get("track_index", action.get("track_idx", 0)),
                               "track index")
            for aux_kind, aux_idx, track in self._iter_aux_tracks():
                if aux_kind == kind and aux_idx == idx:
                    return track, None
            raise ValueError("%s track %d not found" % (kind, idx))
        try:
            selected = self._doc.view.selected_track
            idx = self._track_index(selected)
            return selected, idx
        except Exception:
            pass
        raise ValueError("track target is required")

    def _track_by_index(self, raw):
        idx = self._ai_int(raw, "track index")
        if idx < 0 or idx >= len(self._doc.tracks):
            raise ValueError("track index out of range")
        return self._doc.tracks[idx], idx

    def _ai_target_scene(self, action):
        ref = action.get("scene")
        if isinstance(ref, dict):
            if "index" in ref:
                return self._scene_by_index(ref.get("index"))
            if ref.get("id"):
                idx = self._resolve_scene(ref)
                if idx is None:
                    raise ValueError("scene id not found")
                return self._doc.scenes[idx], idx
        for key in ("scene_index", "scene_idx", "index"):
            if key in action:
                return self._scene_by_index(action.get(key))
        if "scene_id" in action:
            idx = self._resolve_scene({"id": action.get("scene_id")})
            if idx is None:
                raise ValueError("scene id not found")
            return self._doc.scenes[idx], idx
        try:
            selected = self._doc.view.selected_scene
            for idx, scene in enumerate(self._doc.scenes):
                if scene == selected:
                    return scene, idx
        except Exception:
            pass
        raise ValueError("scene target is required")

    def _scene_by_index(self, raw):
        idx = self._ai_int(raw, "scene index")
        if idx < 0 or idx >= len(self._doc.scenes):
            raise ValueError("scene index out of range")
        return self._doc.scenes[idx], idx

    def _ai_target_clip_slot(self, action):
        track, _track_idx = self._ai_target_track(action)
        scene, scene_idx = self._ai_target_scene(action)
        try:
            slots = track.clip_slots
            if scene_idx >= len(slots):
                raise ValueError("scene is outside the track slots")
            return track, scene, slots[scene_idx]
        except ValueError:
            raise
        except Exception as e:
            raise ValueError("clip slot could not be resolved: %r" % (e,))

    def _ai_replace_clip_notes(self, action):
        track, scene, slot = self._ai_target_clip_slot(action)
        notes = action.get("notes")
        if not isinstance(notes, list):
            raise ValueError("notes must be a list")
        length = self._ai_clip_length(action)
        if not slot.has_clip:
            slot.create_clip(length)
        clip = slot.clip
        if not clip.is_midi_clip:
            raise ValueError("target slot is not a MIDI clip")
        self._ai_set_optional_name_color(clip, action)
        normal = []
        end_time = length
        for raw in notes:
            note = self._normal_note(raw)
            if note is None:
                raise ValueError("invalid note %r" % (raw,))
            normal.append(note)
            end_time = max(end_time, note["start_time"] + note["duration"])
        current_records = self._clip_note_records(clip)
        for note, _note_id in current_records:
            end_time = max(end_time, note["start_time"] + note["duration"])
        specs = tuple(self._make_note_spec(note) for note in normal)
        ids = tuple(note_id for _note, note_id in current_records if note_id is not None)
        if ids:
            clip.remove_notes_by_id(ids)
        elif current_records:
            clip.remove_notes_extended(0, 128, 0.0, max(end_time, 0.001))
        if specs:
            clip.add_new_notes(specs)
        return {"notes": len(normal), "length": length}

    def _ai_set_device_parameter(self, action):
        if isinstance(action.get("path"), list):
            parameter = self._ai_resolve_path(action.get("path"))
        else:
            track, _idx = self._ai_target_track(action)
            try:
                devices = list(track.devices)
            except Exception:
                devices = []
            device_idx = self._ai_int(action.get("device_index", action.get("device_idx", 0)),
                                      "device index")
            if device_idx < 0 or device_idx >= len(devices):
                raise ValueError("device index out of range")
            device = devices[device_idx]
            parameter = self._ai_find_parameter(device, action)
        value = self._ai_float(action.get("value"), "value")
        try:
            value = max(float(parameter.min), min(float(parameter.max), value))
        except Exception:
            pass
        parameter.value = value
        return self._ai_parameter_summary(parameter)

    def _ai_find_parameter(self, device, action):
        try:
            parameters = list(device.parameters)
        except Exception:
            parameters = []
        if "parameter_index" in action or "parameter_idx" in action:
            idx = self._ai_int(action.get("parameter_index", action.get("parameter_idx")),
                               "parameter index")
            if idx < 0 or idx >= len(parameters):
                raise ValueError("parameter index out of range")
            return parameters[idx]
        name = action.get("parameter", action.get("parameter_name"))
        if not isinstance(name, str):
            raise ValueError("parameter or parameter_index is required")
        ordinal = self._ai_int(action.get("parameter_ordinal", 0), "parameter ordinal")
        seen = 0
        for parameter in parameters:
            if self._device_parameter_name(parameter) == name or self._safe_name(parameter) == name:
                if seen == ordinal:
                    return parameter
                seen += 1
        raise ValueError("parameter %r not found" % (name,))

    def _ai_resolve_path(self, path):
        if not isinstance(path, list) or len(path) > 32:
            raise ValueError("path must be a list with at most 32 tokens")
        obj = self._doc
        tokens = list(path)
        if tokens and tokens[0] == "song":
            tokens = tokens[1:]
        elif tokens and tokens[0] == "app":
            obj = Live.Application.get_application()
            tokens = tokens[1:]
        for token in tokens:
            if isinstance(token, bool):
                raise ValueError("boolean path token is not allowed")
            if isinstance(token, int):
                obj = obj[token]
                continue
            if isinstance(token, str):
                if not token or token.startswith("_"):
                    raise ValueError("private LOM attributes are not allowed")
                obj = getattr(obj, token)
                continue
            raise ValueError("path token %r is not supported" % (token,))
        return obj

    def _ai_serialize(self, value, depth=0):
        if depth > 3:
            return repr(type(value))
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return value[:1000]
        if isinstance(value, (list, tuple)):
            out = [self._ai_serialize(v, depth + 1) for v in list(value)[:64]]
            if len(value) > 64:
                out.append({"truncated": len(value) - 64})
            return out
        if isinstance(value, dict):
            out = {}
            for idx, key in enumerate(value):
                if idx >= 64:
                    out["truncated"] = len(value) - 64
                    break
                if isinstance(key, str):
                    out[key] = self._ai_serialize(value[key], depth + 1)
            return out
        out = {"lom_type": value.__class__.__name__}
        name = self._safe_name(value)
        if name:
            out["name"] = name
        color = self._safe_color(value)
        if color is not None:
            out["color"] = color
        for attr in ("class_name", "class_display_name"):
            try:
                out[attr] = str(getattr(value, attr))
            except Exception:
                pass
        try:
            out.update(self._ai_parameter_summary(value))
        except Exception:
            pass
        return out

    def _ai_set_optional_name_color(self, obj, action):
        if isinstance(action.get("name"), str):
            obj.name = action["name"]
        if "color" in action:
            obj.color = self._ai_color(action.get("color"))

    def _ai_clip_length(self, action):
        length = self._ai_float(action.get("length", action.get("duration", NOTE_TIME_SPAN)),
                                "length")
        if not math.isfinite(length) or length <= 0:
            raise ValueError("length must be a positive finite number")
        return min(length, CLIP_LENGTH_MAX)

    def _ai_color(self, value):
        color = self._ai_int(value, "color")
        if color < 0 or color > 0xFFFFFF:
            raise ValueError("color must be 0x000000..0xFFFFFF")
        return color

    def _ai_insert_index(self, value, size):
        if value is None:
            return size
        idx = self._ai_int(value, "index")
        return max(0, min(size, idx))

    def _ai_int(self, value, label):
        if isinstance(value, bool):
            raise ValueError("%s must be an integer" % label)
        try:
            return int(value)
        except Exception:
            raise ValueError("%s must be an integer" % label)

    def _ai_float(self, value, label):
        if isinstance(value, bool):
            raise ValueError("%s must be a number" % label)
        try:
            number = float(value)
        except Exception:
            raise ValueError("%s must be a number" % label)
        if math.isnan(number) or math.isinf(number):
            raise ValueError("%s must be finite" % label)
        return number

    # ------------------------------------------------------------- snapshots

    def _prime_mirror(self, transport=True):
        """Р—Р°РїРѕРІРЅСЋС” РґР·РµСЂРєР°Р»Рѕ РїРѕС‚РѕС‡РЅРёРј СЃС‚Р°РЅРѕРј, С‰РѕР± РЅР° СЃС‚Р°СЂС‚С– РЅРµ РІРёСЃС‚СЂРµР»РёС‚Рё РїР°С‡РєРѕСЋ РїРѕРґС–Р№."""
        if transport:
            self._mirror["playing"] = bool(self._doc.is_playing)
            self._mirror["tempo"] = round(float(self._doc.tempo), 6)
        for i, track in enumerate(self._doc.tracks):
            try:
                self._mirror["psi"][i] = self._norm_psi(track.playing_slot_index)
            except Exception:
                pass

    def _prime_metadata(self):
        """Capture Track/Scene/Session Clip labels and colors without startup events."""
        self._mirror["meta"] = {}
        if not self._registry_ready:
            return

        def prime(kind, obj, track=None, scene=None):
            address = self._metadata_address(kind, obj, track, scene)
            if address is None:
                return
            for prop in ("name", "color"):
                value = self._metadata_value(obj, prop)
                if value is not None:
                    self._mirror["meta"][self._metadata_key(address, prop)] = value

        for track in self._iter_device_tracks():
            prime("track", track, track=track)
        scenes = list(self._doc.scenes)
        for scene in scenes:
            prime("scene", scene, scene=scene)
        for track in self._doc.tracks:
            try:
                slots = list(track.clip_slots)
            except Exception:
                continue
            for i, scene in enumerate(scenes):
                if i >= len(slots):
                    break
                try:
                    if slots[i].has_clip:
                        prime("clip", slots[i].clip, track=track, scene=scene)
                except Exception:
                    pass

    def _prime_mixer(self):
        """Р—Р°РїРѕРІРЅСЋС” РґР·РµСЂРєР°Р»Рѕ РјС–РєС€РµСЂР° РїС–СЃР»СЏ Р±СѓС‚СЃС‚СЂР°РїСѓ СЂРµС”СЃС‚СЂСѓ.

        Р‘РµР· С†СЊРѕРіРѕ РїРµСЂС€РёР№ Р¶Рµ СЂСѓС… Р±СѓРґСЊ-СЏРєРѕРіРѕ С„РµР№РґРµСЂР° РІРёРіР»СЏРґР°РІ Р±Рё СЏРє Р·РјС–РЅР° РІС–РґРЅРѕСЃРЅРѕ
        None С– РїРѕСЂРѕРґР¶СѓРІР°РІ РїРѕРґС–СЋ -- Р° РЅР° СЃС‚Р°СЂС‚С– С‚Р°РєРёС… В«Р·РјС–РЅВ» РѕРґСЂР°Р·Сѓ РґРµСЃСЏС‚РєРё.
        """
        self._mirror["mix"] = {}
        for track in self._iter_device_tracks():
            track_ref = self._device_track_ref(track)
            if not track_ref:
                continue
            for param, idx in self._mix_slots(track):
                p = self._mix_param(track, param, idx)
                if p is not None:
                    try:
                        self._mirror["mix"][self._mix_key(track_ref, param, idx)] = round(float(p.value), 6)
                    except Exception:
                        pass
            for prop in self._toggle_props(track):
                try:
                    self._mirror["mix"][self._toggle_key(track_ref, prop)] = bool(getattr(track, prop))
                except Exception:
                    pass

    def _prime_devices(self):
        """Prime all ordinary/Return/Master device trees without startup events."""
        self._mirror["device"] = {}
        for track in self._iter_device_tracks():
            track_ref = self._device_track_ref(track)
            if not track_ref:
                continue
            for container, device, chain_path in self._iter_track_devices(track):
                device_ref = self._device_ref(container, device)
                if device_ref is None:
                    continue
                try:
                    parameters = list(device.parameters)
                except Exception:
                    continue
                for parameter in parameters:
                    parameter_ref = self._device_parameter_ref(device, parameter)
                    if parameter_ref is None:
                        continue
                    try:
                        value = float(parameter.value)
                    except Exception:
                        continue
                    if math.isnan(value) or math.isinf(value):
                        continue
                    key = self._device_key(track_ref, chain_path, device_ref, parameter_ref)
                    self._mirror["device"][key] = round(value, 6)

    def _mix_slots(self, track):
        slots = [("volume", None), ("panning", None)]
        kind = self._aux_kind_of(track)
        if kind == "master":
            slots.extend((("crossfader", None), ("cue_volume", None)))
            return slots
        if kind == "return":
            return slots
        try:
            for i in range(len(track.mixer_device.sends)):
                slots.append(("send", i))
        except Exception:
            pass
        return slots

    def _toggle_props(self, track):
        kind = self._aux_kind_of(track)
        if kind == "master":
            return ()
        if kind == "return":
            return ("mute", "solo")
        return ("mute", "solo", "arm")

    # ------------------------------------------------------------ full state

    def _queue_state(self, request_id=None):
        """Збирає повний стан і ставить його в чергу на відправку чанками."""
        state = self._full_state()
        try:
            blob = json.dumps(state, sort_keys=True, ensure_ascii=True,
                              separators=(",", ":"))
        except Exception as e:
            self._warn("state: не серіалізується (%r)" % (e,))
            return
        self._state_id += 1
        sid = self._state_id if request_id is None else request_id
        chunks = [blob[i:i + STATE_CHUNK_CHARS]
                  for i in range(0, len(blob), STATE_CHUNK_CHARS)] or [""]
        self._state_queue = [
            {"m": "state_chunk", "id": sid, "seq": i, "total": len(chunks),
             "chars": len(blob), "data": chunk}
            for i, chunk in enumerate(chunks)
        ]
        self._log("state: %d symbols in %d chunks (id=%s)" % (len(blob), len(chunks), sid))

    def _flush_state(self):
        """Чанки йдуть порціями по тіках. Залп у сотню датаграм переповнив би
        приймальний буфер daemon, і UDP тихо викинув би частину, а зібраний
        наполовину стан гірший за відсутній."""
        if not self._state_queue:
            return
        for _ in range(STATE_CHUNKS_PER_TICK):
            if not self._state_queue:
                return
            if not self._link.send(self._state_queue[0]):
                return  # daemon мовчить, спробуємо на наступному тіку
            self._state_queue.pop(0)

    def _full_state(self):
        """Повний знімок усього, що bridge уміє синхронізувати.

        Адреси ті самі, що й у подіях: uuid треків і сцен, chain_path, сигнатура
        девайса плюс ordinal. Тому знімок не потребує окремої мови опису стану,
        він читається тими самими шляхами, якими застосовується подія.
        """
        try:
            doc_scenes = list(self._doc.scenes)
        except Exception:
            doc_scenes = []
        scenes = []
        for idx, scene in enumerate(doc_scenes):
            sid = self._scenes_reg.id_of(scene, create=False)
            if not sid:
                continue
            scenes.append({
                "id": sid,
                "idx": idx,
                "name": self._safe_name(scene),
                "color": self._safe_color(scene),
            })

        try:
            doc_tracks = list(self._doc.tracks)
        except Exception:
            doc_tracks = []
        tracks = []
        for idx, track in enumerate(doc_tracks):
            tid = self._tracks_reg.id_of(track, create=False)
            if not tid or tid in self._unshared_tracks:
                continue  # без uuid обʼєкт неадресовний, група -- неспільна
            tracks.append({
                "id": tid,
                "idx": idx,
                "name": self._safe_name(track),
                "color": self._safe_color(track),
                "kind": self._track_kind(track),
                "group": self._group_of(track),
                "mixer": self._state_mixer(track),
                "devices": self._state_devices(track),
                "clips": self._state_clips(track, doc_scenes),
            })

        aux_tracks = []
        for kind, idx, track in self._iter_aux_tracks():
            aid = self._aux_tracks_reg.id_of(track, create=False)
            if not aid:
                continue
            aux_tracks.append({
                "id": aid,
                "kind": kind,
                "idx": idx,
                "name": self._safe_name(track),
                "color": self._safe_color(track),
                "mixer": self._state_mixer(track),
                "devices": self._state_devices(track),
            })

        try:
            tempo = round(float(self._doc.tempo), 6)
        except Exception:
            tempo = None
        try:
            playing = bool(self._doc.is_playing)
        except Exception:
            playing = None

        return {
            "version": STATE_VERSION,
            "script": SCRIPT_VERSION,
            "live": self._live_version(),
            "at": time.time(),
            "tempo": tempo,
            "playing": playing,
            "tracks": tracks,
            "aux_tracks": aux_tracks,
            "scenes": scenes,
        }

    def _state_mixer(self, track):
        mixer = {}
        for param, idx in self._mix_slots(track):
            parameter = self._mix_param(track, param, idx)
            if parameter is None:
                continue
            try:
                value = round(float(parameter.value), 6)
            except Exception:
                continue
            if param == "send":
                mixer.setdefault("sends", []).append({"index": idx, "value": value})
            else:
                mixer[param] = value
        for prop in self._toggle_props(track):
            try:
                mixer[prop] = bool(getattr(track, prop))
            except Exception:
                pass
        return mixer

    def _state_devices(self, track):
        devices = []
        for container, device, chain_path in self._iter_track_devices(track):
            ref = self._device_ref(container, device)
            if ref is None:
                continue
            try:
                items = list(device.parameters)
            except Exception:
                items = []
            parameters = []
            for parameter in items:
                pref = self._device_parameter_ref(device, parameter)
                if pref is None:
                    continue
                try:
                    pref["value"] = round(float(parameter.value), 6)
                except Exception:
                    continue
                parameters.append(pref)
            entry = {"device": ref, "parameters": parameters}
            if chain_path:
                entry["chain_path"] = chain_path
            devices.append(entry)
        return devices

    def _state_clips(self, track, scenes):
        clips = []
        try:
            slots = list(track.clip_slots)
        except Exception:
            return clips
        for i, scene in enumerate(scenes):
            if i >= len(slots):
                break
            sid = self._scenes_reg.id_of(scene, create=False)
            if not sid:
                continue
            try:
                slot = slots[i]
                if not slot.has_clip:
                    continue
                clip = slot.clip
            except Exception:
                continue
            if self._clip_is_recording(slot, clip):
                continue  # у польоті: довжина ще заглушкова
            entry = {"scene": {"id": sid}, "clip": self._clip_meta(clip)}
            loop = self._clip_loop_state(clip)
            if loop:
                entry["loop"] = loop
            try:
                if clip.is_midi_clip:
                    entry["notes"] = self._clip_notes(clip)
            except Exception:
                pass
            clips.append(entry)
        return clips

    # ----------------------------------------------------------- state apply

    def _start_state_apply(self, path, request_id=None):
        """Читає знімок із файлу і ставить його в чергу як послідовність подій.

        Файлом, а не датаграмами: daemon і bridge живуть на одній машині, тож
        другий бік чанкування був би тут зайвою копією коду.
        """
        if not isinstance(path, str) or not path:
            return self._warn("state apply: не вказано шлях")
        try:
            size = os.path.getsize(path)
        except Exception as e:
            return self._warn("state apply: %r" % (e,))
        if size > STATE_APPLY_MAX_BYTES:
            return self._warn("state apply: файл завеликий (%d bytes)" % size)
        try:
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception as e:
            return self._warn("state apply: не читається (%r)" % (e,))
        if not isinstance(state, dict):
            return self._warn("state apply: очікував JSON-обʼєкт")

        ops = self._state_to_ops(state)
        self._apply_queue = ops
        self._apply_report = {"id": request_id, "total": len(ops), "ok": 0,
                              "skipped": 0, "failed": 0, "errors": [],
                              "missing": {}, "missing_more": 0}
        # Розбіжність структури -- не пропущена операція, тож іде в missing
        # окремо від _op_gap і без впливу на лічильники.
        for gap in (self._safe(self._structural_gaps, state) or []):
            self._note_gap(gap)
        self._log("state apply: %d ops queued" % len(ops))
        if not ops:
            self._finish_state_apply()

    def _flush_state_apply(self):
        """Порціями по тіках: знімок великого сету -- це тисячі записів у LOM,
        і зробити їх одним махом означає підвісити Live."""
        if not self._apply_queue:
            return
        for _ in range(STATE_APPLY_PER_TICK):
            if not self._apply_queue:
                break
            etype, payload = self._apply_queue.pop(0)
            gap = self._safe(self._op_gap, etype, payload)
            if gap is not None:
                self._apply_report["skipped"] += 1
                self._note_gap(gap)
                continue
            try:
                self._apply(etype, payload, "state")
                self._apply_report["ok"] += 1
            except Exception as e:
                self._apply_report["failed"] += 1
                if len(self._apply_report["errors"]) < 20:
                    self._apply_report["errors"].append("%s: %r" % (etype, e))
        if not self._apply_queue:
            self._finish_state_apply()

    def _finish_state_apply(self):
        report = self._apply_report
        self._apply_report = None
        if report is None:
            return
        missing = sorted(report["missing"].values(), key=lambda item: -item["count"])
        self._log("state apply: done %d/%d (%d skipped, %d failed)"
                  % (report["ok"], report["total"], report["skipped"], report["failed"]))
        self._link.send({
            "m": "state_applied",
            "id": report["id"],
            "total": report["total"],
            "ok": report["ok"],
            "skipped": report["skipped"],
            "failed": report["failed"],
            "missing": missing,
            "missing_more": report["missing_more"],
            "errors": report["errors"],
        })

    def _structural_gaps(self, state):
        """Розбіжності структури, які подіями не лікуються.

        Це не пропущені операції, тож лічильники ok/skipped не чіпаємо:
        тут не «не вдалось застосувати», а «у нас різна розкладка».
        """
        gaps = []
        for track in (state.get("tracks") or []):
            uid = track.get("id")
            if not uid:
                continue
            local, _ref = self._resolve_device_track({"id": uid})
            if local is None:
                continue  # відсутній трек уже опише _op_gap
            theirs = track.get("group") or None
            mine = self._group_of(local)
            if bool(theirs) == bool(mine):
                continue
            gaps.append({
                "what": "group",
                "track": track.get("names", {}).get("track") or self._safe_name(local),
                "name": (theirs or mine or {}).get("name"),
                "here": bool(mine),
            })
            if len(gaps) >= MISSING_LIMIT:
                break
        return gaps

    def _op_gap(self, etype, payload):
        """Чого бракує для цієї операції. None -- усе на місці.

        Перевірка окремою прохідкою навмисно: _apply на нерозвʼязану адресу
        мовчки виходить (це tombstone-семантика журналу, і вона правильна), тож
        без цього звіт рахував би пропущене як застосоване.
        """
        if etype in ("TempoSet", "TransportSet", "StopAllClips"):
            return None

        track = None
        track_ref = payload.get("track")
        if isinstance(track_ref, dict) and track_ref.get("id"):
            track, _ref = self._resolve_device_track(track_ref)
            if track is None:
                return {"what": "track", "id": track_ref.get("id"),
                        "kind": track_ref.get("kind")}

        if etype == "DeviceParamSet":
            device, parameter = self._resolve_device_parameter(
                track, payload.get("chain_path"), payload.get("device"),
                payload.get("parameter"))
            display = (payload.get("device") or {}).get("class_display_name")
            if device is None:
                return {"what": "device", "track": self._safe_name(track), "device": display}
            if parameter is None:
                return {"what": "parameter", "track": self._safe_name(track),
                        "device": display,
                        "name": (payload.get("parameter") or {}).get("name")}
            return None

        scene_ref = payload.get("scene")
        if isinstance(scene_ref, dict) and scene_ref.get("id"):
            if self._resolve_scene(scene_ref) is None:
                return {"what": "scene", "id": scene_ref.get("id")}
            if etype in ("ClipCreate", "ClipNotesSet", "ClipLoopSet") or payload.get("object") == "clip":
                _track, _scene, slot = self._resolve_clip_slot(payload, "state")
                if slot is None:
                    return {"what": "clip", "track": self._safe_name(track),
                            "scene": scene_ref.get("id")}
        return None

    def _note_gap(self, gap):
        """Однакові прогалини склеюються: 60 параметрів відсутнього девайса --
        це один рядок звіту, а не шістдесят."""
        report = self._apply_report
        key = json.dumps(gap, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        known = report["missing"].get(key)
        if known is None:
            if len(report["missing"]) >= MISSING_LIMIT:
                report["missing_more"] += 1
                return
            known = dict(gap)
            known["count"] = 0
            report["missing"][key] = known
        known["count"] += 1

    def _state_to_ops(self, state):
        """Знімок -> послідовність звичайних подій.

        Окремого шляху запису в LOM немає навмисно: знімок застосовується тим
        самим _apply, що й подія з журналу. Тому глушіння власного ехо, перевірка
        адрес і тиха відмова на неіснуючий обʼєкт працюють без другої копії.
        """
        ops = []
        tempo = state.get("tempo")
        if isinstance(tempo, (int, float)):
            ops.append(("TempoSet", {"bpm": float(tempo)}))

        for track in state.get("tracks") or []:
            ref = {"id": track.get("id")}
            if not ref["id"]:
                continue
            ops.extend(self._meta_ops("track", ref, track))
            ops.extend(self._mixer_ops(ref, track.get("mixer") or {}))
            ops.extend(self._device_ops(ref, track.get("devices") or []))
            ops.extend(self._clip_ops(ref, track.get("clips") or []))

        for aux in state.get("aux_tracks") or []:
            ref = {"id": aux.get("id"), "kind": aux.get("kind")}
            if not ref["id"] or not ref["kind"]:
                continue
            ops.extend(self._meta_ops("track", ref, aux))
            ops.extend(self._mixer_ops(ref, aux.get("mixer") or {}))
            ops.extend(self._device_ops(ref, aux.get("devices") or []))

        for scene in state.get("scenes") or []:
            if scene.get("id"):
                ops.extend(self._meta_ops("scene", {"id": scene["id"]}, scene))

        return ops

    def _meta_ops(self, kind, ref, src):
        ops = []
        for prop in ("name", "color"):
            value = src.get(prop)
            if value is None:
                continue
            payload = {"object": kind, "prop": prop, "value": value}
            payload["scene" if kind == "scene" else "track"] = ref
            ops.append(("ObjectMetaSet", payload))
        return ops

    def _mixer_ops(self, ref, mixer):
        ops = []
        for param in ("volume", "panning", "crossfader", "cue_volume"):
            if param in mixer:
                ops.append(("MixerSet", {"track": ref, "param": param, "value": mixer[param]}))
        for send in mixer.get("sends") or []:
            if send.get("value") is None:
                continue
            ops.append(("MixerSet", {"track": ref, "param": "send",
                                     "index": send.get("index"), "value": send.get("value")}))
        for prop in ("mute", "solo", "arm"):
            if prop in mixer:
                ops.append(("TrackToggle", {"track": ref, "param": prop, "value": bool(mixer[prop])}))
        return ops

    def _device_ops(self, ref, devices):
        ops = []
        for entry in devices:
            device = entry.get("device") or {}
            chain_path = entry.get("chain_path") or []
            for parameter in entry.get("parameters") or []:
                if parameter.get("value") is None:
                    continue
                payload = {
                    "track": ref,
                    "device": device,
                    "parameter": {"name": parameter.get("name"),
                                  "ordinal": parameter.get("ordinal")},
                    "value": parameter.get("value"),
                }
                if chain_path:
                    payload["chain_path"] = chain_path
                ops.append(("DeviceParamSet", payload))
        return ops

    def _clip_ops(self, ref, clips):
        ops = []
        for entry in clips:
            scene = entry.get("scene") or {}
            if not scene.get("id"):
                continue
            meta = entry.get("clip") or {}
            notes = entry.get("notes")
            if notes is not None:
                # Порожній слот заповнюємо: створення кліпу нічого не руйнує,
                # а без нього ноти нема куди класти.
                ops.append(("ClipCreate", {"track": ref, "scene": scene, "clip": meta}))
                for region, part in self._note_regions_for(meta, notes):
                    ops.append(("ClipNotesSet", {
                        "track": ref, "scene": scene, "clip": meta,
                        "region": region, "notes": part,
                    }))
            loop = entry.get("loop")
            if loop:
                payload = {"track": ref, "scene": scene}
                payload.update(loop)
                ops.append(("ClipLoopSet", payload))
            for prop in ("name", "color"):
                if meta.get(prop) is not None:
                    ops.append(("ObjectMetaSet", {
                        "object": "clip", "track": ref, "scene": scene,
                        "prop": prop, "value": meta[prop],
                    }))
        return ops

    @staticmethod
    def _note_regions_for(meta, notes):
        """Ріже ноти на регіони, які впритул укривають кліп починаючи з нуля.

        Один регіон на кліп не годиться: приймальний бік відхиляє payload
        із понад 4096 нотами цілком. А починати треба саме з нуля, бо регіон
        не лише додає ноти, а й чистить те, що лежало в ньому раніше.
        """
        try:
            length = max(float(meta.get("length") or NOTE_TIME_SPAN), 0.001)
        except Exception:
            length = NOTE_TIME_SPAN
        if length > CLIP_LENGTH_MAX:
            # Отруєний знімок дав би один регіон на два роки
            length = NOTE_TIME_SPAN
        ordered = sorted(notes, key=lambda n: (n.get("start_time", 0.0), n.get("pitch", 0)))
        end = length
        for note in ordered:
            try:
                end = max(end, float(note.get("start_time", 0.0)) + 0.001)
            except Exception:
                pass
        whole = {"from_pitch": 0, "pitch_span": 128, "from_time": 0.0,
                 "time_span": round(end, 6)}
        if not ordered:
            return [(whole, [])]

        groups = []
        current = []
        for note in ordered:
            if (len(current) >= NOTES_PER_REGION
                    and note.get("start_time") != current[-1].get("start_time")):
                groups.append(current)
                current = []
            current.append(note)
        groups.append(current)
        if len(groups) == 1:
            return [(whole, groups[0])]

        regions = []
        start = 0.0
        for i, group in enumerate(groups):
            if i == len(groups) - 1:
                stop = end
            else:
                try:
                    stop = float(groups[i + 1][0].get("start_time", end))
                except Exception:
                    stop = end
            if stop <= start:
                stop = start + 0.001
            regions.append(({
                "from_pitch": 0, "pitch_span": 128,
                "from_time": round(start, 6), "time_span": round(stop - start, 6),
            }, group))
            start = stop
        return regions

    # ----------------------------------------------------------- присутність

    def _wire_view(self):
        """Підписка на вид.

        Ці listener'и НЕ йдуть у self._obj_cbs навмисно: `_unwire_tracks()`
        знімає звідти геть усе на кожній зміні структури сету, а `song.view` --
        вічний обʼєкт документа. Інакше вічні підписки перевішувались би дарма,
        ще й із вікном сліпоти між зняттям і поверненням.
        """
        view = getattr(self._doc, "view", None)
        if view is None:
            return
        cb = self._make_view_cb()
        for prop in ("selected_track", "selected_scene",
                     "detail_clip", "highlighted_clip_slot"):
            self._listen(view, prop, cb, store=self._view_cbs)
        try:
            app_view = Live.Application.get_application().view
            self._listen(app_view, "focused_document_view", cb, store=self._view_cbs)
        except Exception:
            pass

    def _unwire_view(self):
        for obj, prop, cb in self._view_cbs:
            try:
                if getattr(obj, "%s_has_listener" % prop)(cb):
                    getattr(obj, "remove_%s_listener" % prop)(cb)
            except Exception:
                pass  # обʼєкт міг померти разом із документом
        self._view_cbs = []

    def _make_view_cb(self):
        def cb():
            self._safe(self._on_view_changed)
        return cb

    def _on_view_changed(self):
        # До бутстрапу реєстру uuid ще не спільні -- партнер не зрозумів би адреси
        if not self._registry_ready:
            return
        now = time.time()
        signature = self._view_signature()
        # Я сам щойно поставив цей вид на прохання партнера. Запис у song.view
        # кличе цей listener СИНХРОННО, ще всередині _apply_view, тож дзеркало
        # тут лише доганяє факт -- відправляти назад нема чого.
        if self._suppress_view or now - self._view_applied_at < VIEW_ECHO_WINDOW:
            self._mirror["view"] = signature
            return
        if signature == self._mirror["view"]:
            return
        self._mirror["view"] = signature
        first = self._view_pending["first"] if self._view_pending else now
        self._view_pending = {"due": now + VIEW_DEBOUNCE_SEC, "first": first}

    def _touch_view(self, force=False):
        """Структура змінилась або реєстр щойно готовий: перерахувати підпис."""
        if not self._registry_ready:
            return
        signature = self._view_signature()
        if not force and signature == self._mirror["view"]:
            return
        self._mirror["view"] = signature
        self._view_pending = {"due": 0.0, "first": time.time()}

    def _flush_view(self):
        pending = self._view_pending
        if not pending:
            return
        now = time.time()
        if now < pending["due"] and now - pending["first"] < VIEW_MAX_HOLD:
            return
        self._view_pending = None
        self._link.send({"m": "view", "view": self._view_payload()})

    @staticmethod
    def _safe_attr(obj, name):
        try:
            return getattr(obj, name)
        except Exception:
            return None

    def _view_signature(self):
        payload = self._view_payload()
        if payload is None:
            return "none"
        track = payload.get("track") or {}
        scene = payload.get("scene") or {}
        clip = payload.get("clip") or {}
        return "%s:%s|%s|%s:%s|%s" % (
            track.get("kind") or "track", track.get("id") or "-",
            scene.get("id") or "-",
            clip.get("track") or "-", clip.get("scene") or "-",
            payload.get("screen") or "-")

    def _view_payload(self):
        """Адреси -- uuid, назви лише для показу: партнер резолвить у себе."""
        view = getattr(self._doc, "view", None)
        if view is None or not self._registry_ready:
            return None
        payload = {"names": {}}
        track = self._safe_attr(view, "selected_track")
        if track is not None:
            ref = self._device_track_ref(track)
            if ref:
                payload["track"] = ref
                payload["names"]["track"] = self._safe_name(track)
        scene = self._safe_attr(view, "selected_scene")
        if scene is not None:
            sid = self._scenes_reg.id_of(scene, create=False)
            if sid:
                payload["scene"] = {"id": sid}
                payload["names"]["scene"] = self._safe_name(scene)
        clip = self._detail_clip_ref(view, track)
        if clip:
            payload["clip"] = clip
        screen = self._focused_screen()
        if screen:
            payload["screen"] = screen
        if "track" not in payload and "scene" not in payload:
            return None
        return payload

    def _detail_clip_ref(self, view, track):
        """Кліп адресується парою (track, scene) -- власного uuid у нього немає.
        Тому Arrangement-кліп неадресовний, і це чесніше, ніж вигадати адресу."""
        clip = self._safe_attr(view, "detail_clip")
        if clip is None or track is None:
            return None
        tid = self._tracks_reg.id_of(track, create=False)
        if not tid:
            return None
        try:
            slots = list(track.clip_slots)
            scenes = list(self._doc.scenes)
        except Exception:
            return None
        for i, slot in enumerate(slots):
            if i >= len(scenes):
                break
            try:
                if not slot.has_clip or slot.clip != clip:
                    continue
            except Exception:
                continue
            sid = self._scenes_reg.id_of(scenes[i], create=False)
            return {"track": tid, "scene": sid} if sid else None
        return None

    @staticmethod
    def _focused_screen():
        try:
            focused = str(Live.Application.get_application().view.focused_document_view)
        except Exception:
            return None
        return "arranger" if "Arranger" in focused else "session"

    def _apply_view(self, msg):
        """Єдине місце, де bridge пише song.view -- на явне прохання партнера.

        Запис виділення нічого в проєкті не змінює, але виділений трек вирішує,
        куди йде MIDI з клавіатури і що моніториться. Тож під запис чужий вид
        не сміє смикати його мовчки.
        """
        doc_view = getattr(self._doc, "view", None)
        if doc_view is None:
            return
        if self._recording_guard():
            if not self._view_guard_logged:
                self._view_guard_logged = True
                self._warn("follow призупинено: трек озброєний, а транспорт грає")
            return
        self._view_guard_logged = False

        view = msg.get("view") or {}
        track = None
        if view.get("track"):
            track, _ref = self._resolve_device_track(view.get("track"))
        sidx = self._resolve_scene(view.get("scene")) if view.get("scene") else None

        # Прапорець і мітка часу виставляються ДО першого запису: listener
        # спрацює синхронно, ще всередині цього виклику, і має мовчати навіть
        # на проміжному стані (трек уже новий, сцена ще стара).
        self._suppress_view = True
        self._view_applied_at = time.time()
        try:
            if track is not None:
                doc_view.selected_track = track
            if sidx is not None:
                scenes = list(self._doc.scenes)
                if sidx < len(scenes):
                    doc_view.selected_scene = scenes[sidx]
                    try:
                        slots = list(track.clip_slots) if track is not None else []
                        if sidx < len(slots):
                            doc_view.highlighted_clip_slot = slots[sidx]
                    except Exception:
                        pass  # у Return/Master слотів немає
        finally:
            self._suppress_view = False
            # Вікно ехо лишається відкритим: Live може "доправити" виділення
            # асинхронно, якщо цільовий обʼєкт саме зник.
            self._view_applied_at = time.time()
            self._mirror["view"] = self._view_signature()
            self._view_pending = None

    def _recording_guard(self):
        try:
            if not self._doc.is_playing:
                return False
            for track in self._doc.tracks:
                if self._safe_attr(track, "arm"):
                    return True
        except Exception:
            return False
        return False

    def _snapshot(self):
        tracks = []
        for i, t in enumerate(self._doc.tracks):
            try:
                tracks.append({
                    "idx": i,
                    "name": self._safe_name(t),
                    "playing_slot_index": self._norm_psi(t.playing_slot_index),
                    "slots": len(t.clip_slots),
                })
            except Exception:
                pass
        scenes = []
        for i, s in enumerate(self._doc.scenes):
            scenes.append({"idx": i, "name": self._safe_name(s)})
        try:
            file_path = str(self._doc.file_path)
        except Exception:
            file_path = ""
        return {
            "playing": bool(self._doc.is_playing),
            "tempo": round(float(self._doc.tempo), 6),
            "file_path": file_path,  # daemon РІРёРІРѕРґРёС‚СЊ С–Р· РЅСЊРѕРіРѕ С‚РµРєСѓ РїСЂРѕС”РєС‚Сѓ
            "samples": self._safe(self._scan_samples) or {},
            "tracks": tracks,
            "scenes": scenes,
        }

    # -------------------------------------------------------------- plumbing

    def _show_message(self, text):
        try:
            self.show_message(text)
        except Exception:
            pass

    def _emit(self, etype, payload):
        self._lseq += 1
        self._link.send({"m": "event", "type": etype, "payload": payload, "lseq": self._lseq})
        self._log("-> %s %r" % (etype, payload))

    def _live_version(self):
        try:
            app = Live.Application.get_application()
            return "%d.%d.%d" % (app.get_major_version(),
                                 app.get_minor_version(),
                                 app.get_bugfix_version())
        except Exception:
            return "unknown"

    def _safe(self, fn, *args):
        try:
            return fn(*args)
        except Exception:
            self._log_exc(getattr(fn, "__name__", "?"))
        return None

    def _warn(self, text):
        self._log("WARN " + text)
        if self._link is not None:
            self._link.send({"m": "log", "level": "warn", "text": text})

    def _log_exc(self, where):
        self._log("EXC in %s:\n%s" % (where, traceback.format_exc()))

    def _log(self, text):
        line = "[%s] %s" % (time.strftime("%H:%M:%S"), text)
        try:
            self.log_message("AbletonMP: " + text)
        except Exception:
            pass
        if not self._log_file:
            return
        try:
            if os.path.exists(self._log_file) and os.path.getsize(self._log_file) > LOG_MAX_BYTES:
                os.remove(self._log_file)
            with open(self._log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
