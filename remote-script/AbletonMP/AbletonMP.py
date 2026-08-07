# -*- coding: utf-8 -*-
"""AbletonMP -- тонкий bridge між Live Object Model і локальним daemon.

Інваріант цього файлу: **звідси ніколи не вилітає виняток у Live**. Кожен callback
з боку Live і кожен tick загорнуті в _safe(). Вся логіка, яку можна винести назовні,
винесена в daemon.

Фаза 1: transport (play/stop), tempo, clip launch/stop.
"""

import json
import os
import time
import traceback

import Live

try:
    from ableton.v2.control_surface import ControlSurface
except ImportError:  # старіші/інші збірки
    from _Framework.ControlSurface import ControlSurface

from .link import UdpLink
from .registry import Registry

SCRIPT_VERSION = "0.6.2"
HEARTBEAT_SEC = 2.0
LOG_MAX_BYTES = 512 * 1024

# Дебаунс неперервних параметрів: журнал має нести дії користувача, а не кожен
# крок ручки. DEBOUNCE_SEC -- тиша після останньої зміни, після якої жест
# вважається завершеним. DEBOUNCE_MAX_HOLD -- стеля: під час довгого безперервного
# жесту подія все одно йде раз на секунду, щоб хвилинний рух не пропав при розриві
# (той самий checkpoint, що й у vision.md §5.5).
DEBOUNCE_SEC = 0.2
DEBOUNCE_MAX_HOLD = 1.0

# Ключі для set_data/get_data -- зберігання всередині самого .als.
# Пріоритет за DATA_KEY_OBJ: uuid лежить на самому об'єкті, тож переживає
# переставляння треків між сесіями. DATA_KEY_MAP -- фолбек однією мапою на Song,
# якщо об'єкти не підтримують set_data; він прив'язаний до позицій і слабший.
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
        self._mirror = {"playing": None, "tempo": None, "psi": {}}
        self._track_cbs = []
        self._pending = {}   # key -> відкладена подія, схлопується за ключем
        self._clip_buf = {}  # track_idx -> psi, накопичується між тіками
        self._tracks_reg = Registry(self._log)
        self._scenes_reg = Registry(self._log)
        # до бутстрапу uuid ще не спільні з партнером, тож події з посиланнями
        # на об'єкти нікуди не відправляємо -- вони б у нього не зарезолвились
        self._registry_ready = False
        # поки застосовуємо чужу структурну подію, свій listener має мовчати:
        # інакше створений трек одразу поїхав би назад як власний TrackCreate
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
        self._rewire_tracks()
        self._prime_mirror()

        self._link.send({
            "m": "hello",
            "live": self._live_version(),
            "script": SCRIPT_VERSION,
            "pid": os.getpid(),
        })
        self._link.send({"m": "snapshot", "state": self._snapshot()})
        self._log("AbletonMP %s connected, Live %s" % (SCRIPT_VERSION, self._live_version()))
        self._safe(self._probe_persistence)

    def _probe_persistence(self):
        """Що доступно для зберігання реєстру в цій збірці Live.

        Нічого не пише -- лише дивиться. Друга машина може мати іншу версію Live,
        і тоді цей рядок у лозі одразу пояснює, чому реєстр не пережив сесію.
        """
        caps = {
            "song.set_data": hasattr(self._doc, "set_data"),
            "track.set_data": bool(self._doc.tracks) and hasattr(self._doc.tracks[0], "set_data"),
            "scene.set_data": bool(self._doc.scenes) and hasattr(self._doc.scenes[0], "set_data"),
        }
        try:
            caps["file_path"] = str(self._doc.file_path) or "(не збережено)"
        except Exception:
            caps["file_path"] = "(недоступно)"
        self._log("persistence: %r" % (caps,))
        self._link.send({"m": "log", "level": "info", "text": "persistence: %r" % (caps,)})

    def disconnect(self):
        self._safe(self._teardown)
        ControlSurface.disconnect(self)

    def _teardown(self):
        # незавершений жест не має пропасти разом із закриттям Live
        self._safe(self._flush_clips)
        self._safe(self._flush_pending, True)
        self._unwire_tracks()
        if self._doc is not None:
            for name, cb in (("is_playing", self._cb_is_playing),
                             ("tempo", self._cb_tempo),
                             ("tracks", self._cb_tracks),
                             ("scenes", self._cb_scenes)):
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

    def _rewire_tracks(self):
        self._unwire_tracks()
        for track in self._doc.tracks:
            try:
                cb = self._make_slot_cb(track)
                track.add_playing_slot_index_listener(cb)
                self._track_cbs.append((track, cb))
            except Exception:
                pass  # трек без слотів -- не наша проблема

    def _unwire_tracks(self):
        for track, cb in self._track_cbs:
            try:
                if track.playing_slot_index_has_listener(cb):
                    track.remove_playing_slot_index_listener(cb)
            except Exception:
                pass  # трек уже видалений -- звертання до нього кидає RuntimeError
        self._track_cbs = []

    def _make_slot_cb(self, track):
        def cb():
            self._safe(self._on_playing_slot, track)
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
            return  # це відлуння нашого власного apply
        self._mirror["playing"] = playing
        self._emit("TransportSet", {"playing": playing})

    def _on_tempo(self):
        bpm = round(float(self._doc.tempo), 6)
        if self._mirror["tempo"] == bpm:
            return
        self._mirror["tempo"] = bpm
        self._defer("tempo", "TempoSet", {"bpm": bpm})

    def _on_tracks(self):
        # структура треків змінилась: перепідписуємось і скидаємо дзеркало слотів,
        # інакше зсув індексів породить фантомні ClipLaunch
        self._rewire_tracks()
        self._mirror["psi"] = {}
        self._clip_buf = {}  # накопичене посилається на старі індекси
        self._prime_mirror(transport=False)
        if self._registry_ready:
            self._diff_tracks(emit=not self._suppress_struct)

    def _on_scenes(self):
        if self._registry_ready:
            self._diff_scenes(emit=not self._suppress_struct)

    @staticmethod
    def _norm_psi(value):
        """Live має кілька відʼємних значень для «не грає» (-1 нічого, -2 є fired slot).
        Для журналу це один стан; без нормалізації перехід -1 -> -2 виглядає як
        зупинка кліпу і породжує фантомний ClipStop."""
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
        # Не відправляємо одразу: запуск сцени смикає listener на кожному треку
        # окремо. Накопичуємо до наступного тіку і там вирішуємо, що це було.
        self._clip_buf[idx] = psi

    # -------------------------------------------------------------- registry

    def _build_registry(self):
        """Видає uuid усім об'єктам. Результат стає подією RegistryInit у журналі."""
        self._tracks_reg.clear()
        self._scenes_reg.clear()
        # спершу піднімаємо uuid із самого .als: якщо обидві машини відкрили той
        # самий файл, вони отримають однакові uuid ще до будь-якого обміну
        restored = self._restore_registry()
        reg = {"tracks": [], "scenes": []}
        for i, t in enumerate(self._doc.tracks):
            reg["tracks"].append({"id": self._tracks_reg.id_of(t), "idx": i, "name": self._safe_name(t)})
        for i, s in enumerate(self._doc.scenes):
            reg["scenes"].append({"id": self._scenes_reg.id_of(s), "idx": i, "name": self._safe_name(s)})
        self._registry_ready = True
        self._persist_registry()
        self._log("реєстр створено: %d треків, %d сцен (%d піднято з .als)"
                  % (len(reg["tracks"]), len(reg["scenes"]), restored))
        return reg

    def _adopt_registry(self, reg):
        """Накладає чужі uuid на свої об'єкти за позицією, звіряючи імена.

        Це єдине місце, де індекс ще є адресою -- одноразово, на бутстрапі.
        Далі індекси в протоколі не фігурують взагалі.
        """
        self._tracks_reg.clear()
        self._scenes_reg.clear()
        # uuid, збережені в .als, головніші за позицію: ім'я треку в Live
        # змінюється саме собою від кинутого девайса, тож звірка за іменем
        # відкидала б цілком легітимні збіги
        self._restore_registry()
        problems = []
        by_data = 0
        by_position = 0

        for kind, records, objects, reg_obj in (
            ("трек", reg.get("tracks") or [], self._doc.tracks, self._tracks_reg),
            ("сцена", reg.get("scenes") or [], self._doc.scenes, self._scenes_reg),
        ):
            for rec in records:
                uid = rec.get("id")
                if uid and reg_obj.obj_of(uid) is not None:
                    by_data += 1
                    continue  # цей об'єкт уже впізнав себе сам

                i = rec.get("idx")
                if not isinstance(i, int) or i < 0 or i >= len(objects):
                    problems.append("%s %r: позиції %r тут немає" % (kind, rec.get("name"), i))
                    continue
                want = rec.get("name")
                if want and self._safe_name(objects[i]) != want:
                    problems.append("%s %d: тут %r, у партнера %r"
                                    % (kind, i, self._safe_name(objects[i]), want))
                    continue
                reg_obj.bind(uid, objects[i])
                by_position += 1

        self._registry_ready = True
        # канонічні uuid із журналу лягають у .als, щоб наступного разу проєкт
        # відкрився вже з ними і бутстрап за позиціями не знадобився
        self._persist_registry()
        self._log("реєстр прийнято: %d треків, %d сцен (%d з .als, %d за позицією)"
                  % (len(self._tracks_reg), len(self._scenes_reg), by_data, by_position))
        if problems:
            # проєкти розійшлись; події на незіставлені об'єкти просто не застосуються
            self._warn("бутстрап реєстру, незіставлено %d: %s"
                       % (len(problems), "; ".join(problems[:5])))
        if by_data == 0 and by_position == 0 and (reg.get("tracks") or reg.get("scenes")):
            self._warn("жоден об'єкт не зіставився -- сесія relay належить іншому "
                       "проєкту; потрібна нова сесія (--session)")

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
        """Піднімає uuid, збережені в .als. Повертає кількість відновлених.

        Обидва механізми працюють разом, не замість одного: у Live 12 трек тримає
        set_data, а сцена -- ні, тож частина об'єктів впізнає себе сама, а решту
        доводиться діставати з мапи на Song. Ранній вихід після першого проходу
        залишав би сцени без ідентичності назавжди.
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
                        continue  # об'єкт уже впізнав себе через set_data
                    if rec.get("name") and self._safe_name(objects[i]) != rec["name"]:
                        continue
                    reg.bind(rec.get("id"), objects[i])
                    by_map += 1

        if by_object or by_map:
            self._log("з .als відновлено %d uuid (%d на об'єктах, %d з мапи)"
                      % (by_object + by_map, by_object, by_map))
        return by_object + by_map

    def _persist_registry(self):
        """Кладе поточні uuid у .als. Сет позначається зміненим -- це очікувано.

        Мапа на Song пишеться завжди, а не лише коли пер-об'єктний запис упав:
        Scene.set_data не кидає винятку, але й не доживає до наступного відкриття
        файлу, тож детектувати проблему по exception не можна.
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
            self._warn("реєстр не збережено в .als: %r" % (e,))

    def _track_kind(self, track):
        try:
            return "midi" if track.has_midi_input else "audio"
        except Exception:
            return "audio"

    def _diff_tracks(self, emit=True):
        """Звіряє реєстр із деревом треків після зміни структури."""
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

    # ------------------------------------------------------------ coalescing

    def _defer(self, key, etype, payload):
        """Відкладає подію; повторний виклик з тим самим ключем затирає попередню."""
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
        """Розбирає накопичені зміни слотів у семантичні події.

        Запуск сцени видно як «кілька треків одночасно поїхали на той самий індекс»:
        згортаємо в одну SceneLaunch. Супутні зупинки треків без кліпу в цій сцені
        не відправляємо -- scene.fire() на тому боці відтворить їх сам.
        """
        if not self._clip_buf:
            return
        if not self._registry_ready:
            self._clip_buf = {}
            self._warn("реєстр ще не готовий -- зміни кліпів не відправлено")
            return
        buf, self._clip_buf = self._clip_buf, {}

        launched = [i for i, psi in buf.items() if psi >= 0]
        targets = set(buf[i] for i in launched)
        if len(targets) == 1 and len(launched) >= 2:
            self._emit("SceneLaunch", {"scene": self._scene_ref(targets.pop())})
            return

        # Stop All Clips: усе, що змінилось, зупинилось, і ніде більше нічого не грає.
        # Друга умова обовʼязкова -- без неї дві зупинки поспіль в одному тіку
        # виглядали б як глобальний стоп і заглушили б партнеру решту треків.
        if len(launched) == 0 and len(buf) >= 2:
            if not [v for v in self._mirror["psi"].values() if v >= 0]:
                self._emit("StopAllClips", {})
                return

        tracks = self._doc.tracks
        for idx in sorted(buf):
            if idx >= len(tracks):
                continue  # трек зник між тіком і флашем
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
            self._mirror["playing"] = want  # ДО запису в LOM -- глушимо ехо
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
                self._warn("gseq %s: сцена %r поза межами" % (gseq, payload.get("scene")))
                return
            self._mirror["psi"][idx] = sidx
            track.clip_slots[sidx].fire()

        elif etype == "SceneLaunch":
            sidx = self._resolve_scene(payload.get("scene"))
            if sidx is None:
                self._warn("gseq %s: сцена %r не резолвиться" % (gseq, payload.get("scene")))
                return
            # дзеркало треба звести до того, що станеться ПІСЛЯ fire(): треки з кліпом
            # у цій сцені заграють її, решта зупиняться -- інакше піде ехо
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
                return  # такий трек уже є -- повторне застосування не створює дубль
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
                return  # tombstone: об'єкта вже немає, дія в порожнечу не йде
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
            self._warn("невідомий тип події %r (gseq %s)" % (etype, gseq))

    # --------------------------------------------------------------- pumping

    def update_display(self):
        """Live кличе це ~10 разів на секунду -- наш єдиний надійний tick."""
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
        """Адреса -- uuid. idx і name лишаються тільки для читабельності логів."""
        return {"id": self._tracks_reg.id_of(track), "name": self._safe_name(track)}

    def _scene_ref(self, idx):
        scenes = self._doc.scenes
        if idx < 0 or idx >= len(scenes):
            return {"id": None}
        scene = scenes[idx]
        ref = {"id": self._scenes_reg.id_of(scene)}
        # У сцен Live за замовчуванням імені немає (цифри в UI -- це індекси,
        # не назви), тож порожнє поле не кладемо взагалі.
        name = self._safe_name(scene)
        if name:
            ref["name"] = name
        return ref

    def _resolve_track(self, ref):
        """Повертає (об'єкт, поточний індекс) або (None, None)."""
        if not isinstance(ref, dict):
            return None, None
        uid = ref.get("id")
        track = self._tracks_reg.obj_of(uid) if uid else None
        if track is None:
            # невідомий uuid = об'єкта тут немає або він видалений (tombstone)
            self._warn("трек %r невідомий, подію пропущено" % (uid,))
            return None, None
        return track, self._track_index(track)

    def _resolve_scene(self, ref):
        """Сцену адресуємо uuid, але LOM працює індексами -- вертаємо індекс."""
        if not isinstance(ref, dict):
            return None
        uid = ref.get("id")
        scene = self._scenes_reg.obj_of(uid) if uid else None
        if scene is None:
            self._warn("сцена %r невідома, подію пропущено" % (uid,))
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

    # ------------------------------------------------------------- snapshots

    def _prime_mirror(self, transport=True):
        """Заповнює дзеркало поточним станом, щоб на старті не вистрелити пачкою подій."""
        if transport:
            self._mirror["playing"] = bool(self._doc.is_playing)
            self._mirror["tempo"] = round(float(self._doc.tempo), 6)
        for i, track in enumerate(self._doc.tracks):
            try:
                self._mirror["psi"][i] = self._norm_psi(track.playing_slot_index)
            except Exception:
                pass

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
        return {
            "playing": bool(self._doc.is_playing),
            "tempo": round(float(self._doc.tempo), 6),
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
