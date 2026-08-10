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

SCRIPT_VERSION = "0.15.0"

# Типи, які цей bridge уміє ЗАСТОСУВАТИ. Оголошуються при конекті, щоб розсинхрон
# версій між учасниками (vision.md §8) виявлявся одразу, а не виглядав як
# "синхронізація не працює": подія доходить, але приймальний бік про неї не знає.
APPLY_TYPES = [
    "TransportSet", "TempoSet",
    "ClipLaunch", "ClipStop", "SceneLaunch", "StopAllClips",
    "TrackCreate", "TrackDelete", "SceneCreate", "SceneDelete",
    "MixerSet", "TrackToggle", "DeviceParamSet",
    "ClipCreate", "ClipDelete", "ClipNotesSet",
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
        self._lseq = 0
        self._last_beat = 0.0
        self._mirror = {
            "playing": None, "tempo": None, "psi": {}, "mix": {},
            "device": {}, "notes": {}, "clips": {},
        }
        self._obj_cbs = []  # (РѕР±'С”РєС‚, РЅР°Р·РІР° РІР»Р°СЃС‚РёРІРѕСЃС‚С–, callback)
        self._pending = {}   # key -> РІС–РґРєР»Р°РґРµРЅР° РїРѕРґС–СЏ, СЃС…Р»РѕРїСѓС”С‚СЊСЃСЏ Р·Р° РєР»СЋС‡РµРј
        self._note_pending = {}  # clip key -> {track, scene, clip, due, first}
        self._clip_buf = {}  # track_idx -> psi, РЅР°РєРѕРїРёС‡СѓС”С‚СЊСЃСЏ РјС–Р¶ С‚С–РєР°РјРё
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

        self._doc.add_is_playing_listener(self._cb_is_playing)
        self._doc.add_tempo_listener(self._cb_tempo)
        self._doc.add_tracks_listener(self._cb_tracks)
        self._doc.add_scenes_listener(self._cb_scenes)
        # РїРѕСЏРІР°/Р·РЅРёРєРЅРµРЅРЅСЏ return-С‚СЂРµРєСѓ Р·РјС–РЅСЋС” РєС–Р»СЊРєС–СЃС‚СЊ send-С–РІ РЅР° РєРѕР¶РЅРѕРјСѓ С‚СЂРµРєСѓ,
        # Р° С†Рµ РѕРєСЂРµРјС– listener'Рё -- Р±РµР· С†СЊРѕРіРѕ РЅРѕРІС– send-Рё Р»РёС€РёР»РёСЃСЊ Р±Рё РЅС–РјРёРјРё
        self._doc.add_return_tracks_listener(self._cb_tracks)
        self._rewire_tracks()
        self._prime_mirror()

        self._link.send({
            "m": "hello",
            "live": self._live_version(),
            "script": SCRIPT_VERSION,
            "pid": os.getpid(),
            "events": APPLY_TYPES,
            "features": ["apply_ack"],
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
        self._log("AbletonMP disconnected")

    # ------------------------------------------------------------- listeners

    def _listen(self, obj, prop, cb):
        """РЈР·Р°РіР°Р»СЊРЅРµРЅР° РїС–РґРїРёСЃРєР°: LOM С‚СЂРёРјР°С” С”РґРёРЅСѓ СЃС…РµРјСѓ add_/remove_/_has_listener,
        С‚РѕР¶ РїРµСЂРµР»С–С‡СѓРІР°С‚Рё РєРѕР¶РµРЅ РїР°СЂР°РјРµС‚СЂ РѕРєСЂРµРјРѕ РЅРµ С‚СЂРµР±Р°."""
        try:
            getattr(obj, "add_%s_listener" % prop)(cb)
            self._obj_cbs.append((obj, prop, cb))
        except Exception:
            pass  # РїР°СЂР°РјРµС‚СЂР° С‚СѓС‚ РЅРµРјР°С” (РЅР°РїСЂ. arm РЅР° С‚СЂРµРєСѓ, СЏРєРёР№ РЅРµ РѕР·Р±СЂРѕСЋС”С‚СЊСЃСЏ)

    def _rewire_tracks(self):
        self._unwire_tracks()
        for track in self._doc.tracks:
            self._listen(track, "playing_slot_index", self._make_slot_cb(track))
            self._wire_mixer(track)
            self._wire_devices(track)
            self._wire_note_slots(track)
        for track in self._device_aux_tracks():
            self._wire_devices(track)

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
                if slot.has_clip and slot.clip.is_midi_clip:
                    clip = slot.clip
                    self._listen(clip, "notes", self._make_notes_cb(track, scene, clip))
            except Exception:
                pass

    def _wire_mixer(self, track):
        md = None
        try:
            md = track.mixer_device
        except Exception:
            return
        self._listen(md.volume, "value", self._make_mix_cb(track, "volume", None))
        self._listen(md.panning, "value", self._make_mix_cb(track, "panning", None))
        try:
            sends = list(md.sends)
        except Exception:
            sends = []
        for i, send in enumerate(sends):
            self._listen(send, "value", self._make_mix_cb(track, "send", i))
        for prop in ("mute", "solo", "arm"):
            self._listen(track, prop, self._make_toggle_cb(track, prop))

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

    def _on_scenes(self):
        if self._registry_ready:
            self._flush_notes(True)
            self._diff_scenes(emit=not self._suppress_struct)
            self._rewire_tracks()
            self._prime_mirror(transport=False)
            self._prime_mixer()
            self._prime_devices()
            self._prime_notes()

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
        self._rewire_tracks()
        self._prime_mixer()
        self._prime_devices()
        self._prime_notes()
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
        self._rewire_tracks()
        self._prime_mixer()
        self._prime_devices()
        self._prime_notes()
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
                if uid:
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
            return {"id": tid}
        aid = self._aux_tracks_reg.id_of(track, create=False)
        kind = self._aux_kind_of(track)
        if aid and kind:
            return {"id": aid, "kind": kind}
        return None

    def _iter_device_tracks(self):
        for track in self._doc.tracks:
            yield track
        for track in self._device_aux_tracks():
            yield track

    def _track_kind(self, track):
        try:
            return "midi" if track.has_midi_input else "audio"
        except Exception:
            return "audio"

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
            self._emit("TrackCreate", {
                "track": {"id": uid, "name": self._safe_name(track)},
                "idx": idx,
                "kind": self._track_kind(track),
            })
        for uid in removed:
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
            self._emit("SceneCreate", {"scene": ref, "idx": idx})
        for uid in removed:
            self._emit("SceneDelete", {"scene": {"id": uid}})

    # ----------------------------------------------------------------- mixer

    def _mix_param(self, track, param, idx):
        try:
            md = track.mixer_device
            if param == "volume":
                return md.volume
            if param == "panning":
                return md.panning
            if param == "send":
                sends = list(md.sends)
                return sends[idx] if isinstance(idx, int) and idx < len(sends) else None
        except Exception:
            pass
        return None

    def _mix_key(self, tid, param, idx):
        return "%s:%s:%s" % (tid, param, idx)

    def _on_mix(self, track, param, idx):
        if not self._registry_ready:
            return
        tid = self._tracks_reg.id_of(track, create=False)
        p = self._mix_param(track, param, idx)
        if not tid or p is None:
            return
        value = round(float(p.value), 6)
        key = self._mix_key(tid, param, idx)
        if self._mirror["mix"].get(key) == value:
            return
        self._mirror["mix"][key] = value
        payload = {"track": {"id": tid}, "param": param, "value": value}
        if idx is not None:
            payload["index"] = idx
        # РЅРµРїРµСЂРµСЂРІРЅР° РІРµР»РёС‡РёРЅР° -- РґРµР±Р°СѓРЅСЃРёРјРѕ, СЏРє tempo: СЂСѓС… С„РµР№РґРµСЂР° С†Рµ РѕРґРёРЅ Р¶РµСЃС‚
        self._defer("mix:" + key, "MixerSet", payload)

    def _on_toggle(self, track, prop):
        if not self._registry_ready:
            return
        tid = self._tracks_reg.id_of(track, create=False)
        if not tid:
            return
        try:
            value = bool(getattr(track, prop))
        except Exception:
            return
        key = "%s:%s" % (tid, prop)
        if self._mirror["mix"].get(key) == value:
            return
        self._mirror["mix"][key] = value
        # РґРёСЃРєСЂРµС‚РЅРµ РїРµСЂРµРјРёРєР°РЅРЅСЏ -- РґРµР±Р°СѓРЅСЃ С‚СѓС‚ Р»РёС€Рµ РґРѕРґР°РІ Р±Рё Р·Р°С‚СЂРёРјРєРё
        self._emit("TrackToggle", {"track": {"id": tid}, "param": prop, "value": value})

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
                            uid = (preferred.get(locator_key) or
                                   self._obj_stored_id(chain) or
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
        return {"length": round(length, 6), "name": self._safe_name(clip)}

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
        if key is None:
            return
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
                    kind = "midi" if clip.is_midi_clip else "audio"
                    self._mirror["clips"][key] = kind
                    if kind == "midi":
                        self._mirror["notes"][key] = self._clip_notes(clip)
                except Exception:
                    pass

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
            kind = "midi" if clip.is_midi_clip else "audio"
            self._mirror["clips"][key] = kind
            if kind == "midi":
                self._mirror["notes"][key] = self._clip_notes(clip)
            else:
                self._mirror["notes"].pop(key, None)
        except Exception:
            pass

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
        if not math.isfinite(length) or length <= 0 or length > 1073741824.0:
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
        if isinstance(name, str) and name:
            try:
                clip.name = name
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
            self._suppress_struct = True
            try:
                if payload.get("kind") == "midi":
                    self._doc.create_midi_track(idx)
                else:
                    self._doc.create_audio_track(idx)
                new = self._doc.tracks[idx]
                if ref.get("name"):
                    new.name = ref["name"]
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
                if ref.get("name"):
                    new.name = ref["name"]
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

        elif etype == "ClipNotesSet":
            track, scene, slot = self._resolve_clip_slot(payload, gseq)
            if slot is None:
                return
            self._apply_note_region(track, scene, slot, payload, gseq)

        elif etype == "MixerSet":
            track, _idx = self._resolve_track(payload.get("track"))
            if track is None:
                return
            param = payload.get("param")
            idx = payload.get("index")
            p = self._mix_param(track, param, idx)
            if p is None:
                self._warn("gseq %s: РїР°СЂР°РјРµС‚СЂ %r/%r РІС–РґСЃСѓС‚РЅС–Р№" % (gseq, param, idx))
                return
            tid = self._tracks_reg.id_of(track, create=False)
            try:
                value = float(payload.get("value"))
            except Exception:
                return
            # DeviceParameter РєРёРґР°С” РїСЂРё РІРёС…РѕРґС– Р·Р° РјРµР¶С–, Р° РјРµР¶С– send-С–РІ
            # РІС–РґСЂС–Р·РЅСЏСЋС‚СЊСЃСЏ РІС–Рґ volume -- Р±РµСЂРµРјРѕ С—С… Р· СЃР°РјРѕРіРѕ РїР°СЂР°РјРµС‚СЂР°
            value = max(p.min, min(p.max, value))
            self._mirror["mix"][self._mix_key(tid, param, idx)] = round(value, 6)
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
            track, _idx = self._resolve_track(payload.get("track"))
            if track is None:
                return
            prop = payload.get("param")
            if prop not in ("mute", "solo", "arm"):
                self._warn("gseq %s: РЅРµРІС–РґРѕРјРёР№ РїРµСЂРµРјРёРєР°С‡ %r" % (gseq, prop))
                return
            tid = self._tracks_reg.id_of(track, create=False)
            value = bool(payload.get("value"))
            self._mirror["mix"]["%s:%s" % (tid, prop)] = value
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
        if self._link is None or not self._link.alive:
            return
        for msg in self._link.poll():
            self._safe(self._dispatch, msg)
        self._safe(self._flush_clips)
        self._safe(self._flush_notes)
        self._safe(self._flush_pending)
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
                "features": ["apply_ack"],
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

    def _prime_mixer(self):
        """Р—Р°РїРѕРІРЅСЋС” РґР·РµСЂРєР°Р»Рѕ РјС–РєС€РµСЂР° РїС–СЃР»СЏ Р±СѓС‚СЃС‚СЂР°РїСѓ СЂРµС”СЃС‚СЂСѓ.

        Р‘РµР· С†СЊРѕРіРѕ РїРµСЂС€РёР№ Р¶Рµ СЂСѓС… Р±СѓРґСЊ-СЏРєРѕРіРѕ С„РµР№РґРµСЂР° РІРёРіР»СЏРґР°РІ Р±Рё СЏРє Р·РјС–РЅР° РІС–РґРЅРѕСЃРЅРѕ
        None С– РїРѕСЂРѕРґР¶СѓРІР°РІ РїРѕРґС–СЋ -- Р° РЅР° СЃС‚Р°СЂС‚С– С‚Р°РєРёС… В«Р·РјС–РЅВ» РѕРґСЂР°Р·Сѓ РґРµСЃСЏС‚РєРё.
        """
        self._mirror["mix"] = {}
        for track in self._doc.tracks:
            tid = self._tracks_reg.id_of(track, create=False)
            if not tid:
                continue
            for param, idx in self._mix_slots(track):
                p = self._mix_param(track, param, idx)
                if p is not None:
                    try:
                        self._mirror["mix"][self._mix_key(tid, param, idx)] = round(float(p.value), 6)
                    except Exception:
                        pass
            for prop in ("mute", "solo", "arm"):
                try:
                    self._mirror["mix"]["%s:%s" % (tid, prop)] = bool(getattr(track, prop))
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
        try:
            for i in range(len(track.mixer_device.sends)):
                slots.append(("send", i))
        except Exception:
            pass
        return slots

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
