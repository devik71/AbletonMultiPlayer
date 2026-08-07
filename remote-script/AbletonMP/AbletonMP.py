# -*- coding: utf-8 -*-
"""AbletonMP -- С‚РѕРЅРєРёР№ bridge РјС–Р¶ Live Object Model С– Р»РѕРєР°Р»СЊРЅРёРј daemon.

Р†РЅРІР°СЂС–Р°РЅС‚ С†СЊРѕРіРѕ С„Р°Р№Р»Сѓ: **Р·РІС–РґСЃРё РЅС–РєРѕР»Рё РЅРµ РІРёР»С–С‚Р°С” РІРёРЅСЏС‚РѕРє Сѓ Live**. РљРѕР¶РµРЅ callback
Р· Р±РѕРєСѓ Live С– РєРѕР¶РµРЅ tick Р·Р°РіРѕСЂРЅСѓС‚С– РІ _safe(). Р’СЃСЏ Р»РѕРіС–РєР°, СЏРєСѓ РјРѕР¶РЅР° РІРёРЅРµСЃС‚Рё РЅР°Р·РѕРІРЅС–,
РІРёРЅРµСЃРµРЅР° РІ daemon.

Р¤Р°Р·Р° 1: transport (play/stop), tempo, clip launch/stop.
"""

import json
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

SCRIPT_VERSION = "0.10.0"

# Типи, які цей bridge уміє ЗАСТОСУВАТИ. Оголошуються при конекті, щоб розсинхрон
# версій між учасниками (vision.md §8) виявлявся одразу, а не виглядав як
# "синхронізація не працює": подія доходить, але приймальний бік про неї не знає.
APPLY_TYPES = [
    "TransportSet", "TempoSet",
    "ClipLaunch", "ClipStop", "SceneLaunch", "StopAllClips",
    "TrackCreate", "TrackDelete", "SceneCreate", "SceneDelete",
    "MixerSet", "TrackToggle",
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
        self._mirror = {"playing": None, "tempo": None, "psi": {}, "mix": {}}
        self._obj_cbs = []  # (РѕР±'С”РєС‚, РЅР°Р·РІР° РІР»Р°СЃС‚РёРІРѕСЃС‚С–, callback)
        self._pending = {}   # key -> РІС–РґРєР»Р°РґРµРЅР° РїРѕРґС–СЏ, СЃС…Р»РѕРїСѓС”С‚СЊСЃСЏ Р·Р° РєР»СЋС‡РµРј
        self._clip_buf = {}  # track_idx -> psi, РЅР°РєРѕРїРёС‡СѓС”С‚СЊСЃСЏ РјС–Р¶ С‚С–РєР°РјРё
        self._tracks_reg = Registry(self._log)
        self._scenes_reg = Registry(self._log)
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
        self._rewire_tracks()
        self._mirror["psi"] = {}
        self._clip_buf = {}  # РЅР°РєРѕРїРёС‡РµРЅРµ РїРѕСЃРёР»Р°С”С‚СЊСЃСЏ РЅР° СЃС‚Р°СЂС– С–РЅРґРµРєСЃРё
        self._prime_mirror(transport=False)
        if self._registry_ready:
            self._diff_tracks(emit=not self._suppress_struct)
            self._prime_mixer()  # listener'Рё РјС–РєС€РµСЂР° РїРµСЂРµРІС–С€Р°РЅС– РЅР° РЅРѕРІС– РѕР±'С”РєС‚Рё

    def _on_scenes(self):
        if self._registry_ready:
            self._diff_scenes(emit=not self._suppress_struct)

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
        # СЃРїРµСЂС€Сѓ РїС–РґРЅС–РјР°С”РјРѕ uuid С–Р· СЃР°РјРѕРіРѕ .als: СЏРєС‰Рѕ РѕР±РёРґРІС– РјР°С€РёРЅРё РІС–РґРєСЂРёР»Рё С‚РѕР№
        # СЃР°РјРёР№ С„Р°Р№Р», РІРѕРЅРё РѕС‚СЂРёРјР°СЋС‚СЊ РѕРґРЅР°РєРѕРІС– uuid С‰Рµ РґРѕ Р±СѓРґСЊ-СЏРєРѕРіРѕ РѕР±РјС–РЅСѓ
        restored = self._restore_registry()
        reg = {"tracks": [], "scenes": []}
        for i, t in enumerate(self._doc.tracks):
            reg["tracks"].append({"id": self._tracks_reg.id_of(t), "idx": i, "name": self._safe_name(t)})
        for i, s in enumerate(self._doc.scenes):
            reg["scenes"].append({"id": self._scenes_reg.id_of(s), "idx": i, "name": self._safe_name(s)})
        self._registry_ready = True
        self._prime_mixer()
        self._persist_registry()
        self._log("СЂРµС”СЃС‚СЂ СЃС‚РІРѕСЂРµРЅРѕ: %d С‚СЂРµРєС–РІ, %d СЃС†РµРЅ (%d РїС–РґРЅСЏС‚Рѕ Р· .als)"
                  % (len(reg["tracks"]), len(reg["scenes"]), restored))
        return reg

    def _adopt_registry(self, reg):
        """РќР°РєР»Р°РґР°С” С‡СѓР¶С– uuid РЅР° СЃРІРѕС— РѕР±'С”РєС‚Рё Р·Р° РїРѕР·РёС†С–С”СЋ, Р·РІС–СЂСЏСЋС‡Рё С–РјРµРЅР°.

        Р¦Рµ С”РґРёРЅРµ РјС–СЃС†Рµ, РґРµ С–РЅРґРµРєСЃ С‰Рµ С” Р°РґСЂРµСЃРѕСЋ -- РѕРґРЅРѕСЂР°Р·РѕРІРѕ, РЅР° Р±СѓС‚СЃС‚СЂР°РїС–.
        Р”Р°Р»С– С–РЅРґРµРєСЃРё РІ РїСЂРѕС‚РѕРєРѕР»С– РЅРµ С„С–РіСѓСЂСѓСЋС‚СЊ РІР·Р°РіР°Р»С–.
        """
        self._tracks_reg.clear()
        self._scenes_reg.clear()
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

        self._registry_ready = True
        self._prime_mixer()
        # РєР°РЅРѕРЅС–С‡РЅС– uuid С–Р· Р¶СѓСЂРЅР°Р»Сѓ Р»СЏРіР°СЋС‚СЊ Сѓ .als, С‰РѕР± РЅР°СЃС‚СѓРїРЅРѕРіРѕ СЂР°Р·Сѓ РїСЂРѕС”РєС‚
        # РІС–РґРєСЂРёРІСЃСЏ РІР¶Рµ Р· РЅРёРјРё С– Р±СѓС‚СЃС‚СЂР°Рї Р·Р° РїРѕР·РёС†С–СЏРјРё РЅРµ Р·РЅР°РґРѕР±РёРІСЃСЏ
        self._persist_registry()
        self._log("СЂРµС”СЃС‚СЂ РїСЂРёР№РЅСЏС‚Рѕ: %d С‚СЂРµРєС–РІ, %d СЃС†РµРЅ (%d Р· .als, %d Р·Р° РїРѕР·РёС†С–С”СЋ)"
                  % (len(self._tracks_reg), len(self._scenes_reg), by_data, by_position))
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
                             (self._scenes_reg, self._doc.scenes)):
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
                             (self._scenes_reg, self._doc.scenes)):
            for obj in objects:
                uid = reg.id_of(obj, create=False)
                if uid:
                    self._obj_store_id(obj, uid)

        snap = {"tracks": [], "scenes": []}
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
        self._safe(self._flush_pending)
        now = time.time()
        if now - self._last_beat >= HEARTBEAT_SEC:
            self._last_beat = now
            self._link.send({"m": "heartbeat", "t": now})

    def _dispatch(self, msg):
        m = msg.get("m")
        if m == "apply":
            self._apply(msg.get("type"), msg.get("payload") or {}, msg.get("gseq"))
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
