# -*- coding: utf-8 -*-
"""AbletonMP -- тонкий bridge між Live Object Model і локальним daemon.

Інваріант цього файлу: **звідси ніколи не вилітає виняток у Live**. Кожен callback
з боку Live і кожен tick загорнуті в _safe(). Вся логіка, яку можна винести назовні,
винесена в daemon.

Фаза 1: transport (play/stop), tempo, clip launch/stop.
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
except ImportError:  # старіші/інші збірки
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

SCRIPT_VERSION = "0.19.0"

# Типи, які цей bridge уміє ЗАСТОСУВАТИ. Оголошуються при конекті, щоб розсинхрон
# версій між учасниками (vision.md §8) виявлявся одразу, а не виглядав як
# "синхронізація не працює": подія доходить, але приймальний бік про неї не знає.
APPLY_TYPES = [
    "TransportSet", "TempoSet",
    "ClipLaunch", "ClipStop", "SceneLaunch", "StopAllClips",
    "TrackCreate", "TrackDelete", "TrackDuplicate", "SceneCreate", "SceneDelete",
    "MixerSet", "TrackToggle", "DeviceParamSet", "ObjectMetaSet",
    "ClipCreate", "ClipDelete", "ClipNotesSet", "ClipLoopSet",
    "DeviceLoad",
    "DeviceInsert", "DeviceDelete", "DeviceMove",
    "SampleLoad", "SongPropSet", "SceneTimingSet", "ClipPropSet",
    "CueSet", "CueDelete", "ReturnCreate", "ReturnDelete", "ClipWarpSet",
    "ChainMixerSet",
    "ArrangementClipCreate", "ArrangementClipMove", "ArrangementClipDelete",
    "ArrangementClipNotesSet",
    "SlotStopButtonSet", "SamplePropSet", "DeviceStateSet",
]

# Як емітити перенесення девайса. "move" -- один DeviceMove через
# song.move_device: девайс переїжджає живим, зі своїми значеннями. "pair" --
# DeviceDelete + DeviceInsert: коду менше, але в партнера девайс перестворюється
# з дефолтів. Виміряно на живому 12.4.5b11: Frequency 0.25 -> 0.899657.
# Змінна існує, щоб різницю можна було переміряти, а не доводити.
DEVICE_MOVE_MODE = (os.environ.get("ABLETONMP_DEVICE_MOVE") or "move").strip().lower()
HEARTBEAT_SEC = 2.0
LOG_MAX_BYTES = 512 * 1024

# Дебаунс неперервних параметрів: журнал має нести дії користувача, а не кожен
# крок ручки. DEBOUNCE_SEC -- тиша після останньої зміни, після якої жест
# вважається завершеним. DEBOUNCE_MAX_HOLD -- стеля: під час довгого безперервного
# жесту подія все одно йде раз на секунду, щоб хвилинний рух не пропав при розриві
# (той самий checkpoint, що й у vision.md §5.5).
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
            "presence", "view_follow", "device_load", "arrangement", "sample_load", "song_props", "scene_timing", "clip_props", "cues", "returns", "warp_markers", "chain_mixer", "stop_buttons", "sample_props", "device_state"]

STATE_VERSION = 1
# Чанк знімка мусить лишатись під MAX_DATAGRAM разом із JSON-обгорткою.
STATE_CHUNK_CHARS = 6000
# Чанки поменшали вчетверо, тож за тік їх іде більше -- інакше знімок
# великого сету повз би вп'ятеро довше, ніж досі.
STATE_CHUNKS_PER_TICK = 24
# Застосування знімка: тисячі записів у LOM порціями, щоб не підвісити Live.
STATE_APPLY_PER_TICK = 12
STATE_APPLY_MAX_BYTES = 64 * 1024 * 1024
# Нота в JSON важить ~134 байти, тож 1024 нот -- це 135 КБ: такий пакет не
# влазив навіть у стелю Windows, і будь-який кліп від тисячі нот мовчки
# губився на обох системах. 48 нот -- це ~6.5 КБ, з запасом під обгортку.
NOTES_PER_REGION = 48
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
# Властивості кліпу, що не входять у межі. Частина існує лише в audio
# (gain, warping, warp_mode, pitch_*, ram_mode) -- на MIDI-кліпі їх просто
# немає, і це не помилка, а різниця типів.
CLIP_PROPS = ("gain", "pitch_coarse", "pitch_fine", "warping", "warp_mode",
              "ram_mode", "muted", "legato", "velocity_amount",
              "launch_mode", "launch_quantization",
              # Кліп має ВЛАСНИЙ розмір такту, окремий від пісні: він
              # визначає сітку й те, як читаються позиції нот усередині.
              "signature_numerator", "signature_denominator")

# Стан девайса, якого НЕМАЄ серед parameters.
#
# Виміряно аудитом на живому 12.3.5 (docs/device-audit.md): із 85 стокових
# девайсів 68 не мають нічого понад parameters -- вони покриті DeviceParamSet
# повністю. Решта тримає частину звучання в звичайних властивостях: яка саме
# таблиця в осциляторі Wavetable, який режим програвання в Simpler, який IR
# у Hybrid Reverb, куди йде матриця модуляції в Drift.
#
# Перелік плоский навмисно, без таблиці "клас -> властивості". Імена тут
# достатньо характерні, щоб не перетнутись, а перевірка ведеться через
# hasattr: девайс, у якого властивості немає, просто не потрапляє в підписку.
# Новий девайс потім = рядок у цьому переліку, а не гілка в коді.
# Наскільки має змінитись темп, щоб це вважалось зміною. Ableton Link несе
# темп із похибкою в мільйонні долі BPM, і без порога кожен його дотик
# виглядав як нова подія. Тисячна доля не чутна нікому.
# Наскільки плейхед може не дотягнути до заданої позиції й це ще вважається
# «став туди». Live округлює позицію до своєї сітки, і різниця в тисячні
# долі долі -- це округлення, а не інша позиція.
CUE_POSITION_EPSILON = 0.001

TEMPO_EPSILON = 0.001

DEVICE_STATE_PROPS = (
    # раки: яка варіація підсвічена. Самі значення макросів -- параметри,
    # тож звучання доїжджає й без цього; сюди йде лише вибір.
    "selected_variation_index",
    # Simpler: режим програвання змінює те, ЧИМ девайс є
    "playback_mode", "slicing_playback_mode", "multi_sample_mode",
    "pad_slicing", "retrigger", "voices",
    "pitch_bend_range", "note_pitch_bend_range",
    # Wavetable: яка таблиця в кожному осциляторі -- це і є звук
    "oscillator_1_wavetable_index", "oscillator_1_wavetable_category",
    "oscillator_1_effect_mode",
    "oscillator_2_wavetable_index", "oscillator_2_wavetable_category",
    "oscillator_2_effect_mode",
    "filter_routing", "unison_mode", "unison_voice_count",
    "mono_poly", "poly_voices",
    # Hybrid Reverb: який завантажений IR
    "ir_category_index", "ir_file_index", "ir_attack_time",
    "ir_decay_time", "ir_size_factor", "ir_time_shaping_on",
    # Drift: уся матриця модуляції
    "mod_matrix_source_1_index", "mod_matrix_source_2_index",
    "mod_matrix_source_3_index",
    "mod_matrix_target_1_index", "mod_matrix_target_2_index",
    "mod_matrix_target_3_index",
    "mod_matrix_lfo_source_index", "mod_matrix_shape_source_index",
    "mod_matrix_filter_source_1_index", "mod_matrix_filter_source_2_index",
    "mod_matrix_pitch_source_1_index", "mod_matrix_pitch_source_2_index",
    "voice_count_index", "voice_mode_index",
    # Meld
    "selected_engine", "unison_voices",
    # Spectral Resonator
    "frequency_dial_mode", "midi_gate", "mod_mode", "pitch_mode", "polyphony",
    # EQ Eight
    "edit_mode", "global_mode", "oversample",
    # Roar, Shifter
    "env_listen", "routing_mode_index", "pitch_mode_index",
    # CC Control: на що мапиться кожен CC
    "custom_bool_target",
    "custom_float_target_0", "custom_float_target_1", "custom_float_target_2",
    "custom_float_target_3", "custom_float_target_4", "custom_float_target_5",
    "custom_float_target_6", "custom_float_target_7", "custom_float_target_8",
    "custom_float_target_9", "custom_float_target_10", "custom_float_target_11",
    # Drum Sampler, Looper
    "gain", "loop_length", "overdub_after_record", "record_length_index", "tempo",
)

# Властивості обʼєкта Sample усередині Simpler/Sampler. Це НЕ параметри
# девайса: ручки S Start і S Length -- окремі DeviceParameter, а маркери на
# хвилі живуть тут. Виміряно на живому 12.3.5: усі три пишуться.
#
# start_marker/end_marker -- у семплах (не в долях і не 0..1), тож стеля
# береться з sample.length, а не з константи.
SAMPLE_PROPS = ("start_marker", "end_marker", "gain",
                "warping", "warp_mode", "slicing_beat_division",
                "beats_granulation_resolution")
SAMPLE_BOOL_PROPS = ("warping",)
SAMPLE_INT_PROPS = ("start_marker", "end_marker", "warp_mode",
                    "slicing_beat_division", "beats_granulation_resolution")

# Портативні лише стокові девайси першого рівня: дампи браузера з двох машин
# показали, що їхні uri ідентичні, а вміст адресується локальними FileId.
BROWSER_CATEGORIES = ("audio_effects", "instruments", "midi_effects")
# Скалярні властивості пісні, спільні для всіх учасників.
#
# Петля Arrangement (loop, loop_start, loop_length) -- це "де ми зараз
# працюємо", тобто найспільніше, що взагалі є. punch_in/punch_out ідуть
# із нею: вони змінюють саме те, що ця петля означає для запису.
#
# А от НЕ входять сюди, і кожне з власної причини:
#   record_mode, session_record, arrangement_overdub -- це намір людини
#     натиснути запис, а не стан документа. Приїхавши, вони почали б
#     писати озброєні треки партнера його ж входом -- сюрприз у вигляді
#     чужого дубля;
#   back_to_arranger -- стан ВЛАСНОГО відтворення, у кожного свій;
#   metronome -- особиста річ, як гучність навушників;
#   midi_recording_quantization -- впливає лише на власний запис, і партнер
#     дістає ноти вже квантованими, тож розсинхрону з неї не буває.
SONG_PROPS = ("signature_numerator", "signature_denominator",
              "clip_trigger_quantization", "root_note", "scale_name",
              "scale_mode", "loop", "loop_start", "loop_length", "groove_amount",
              "punch_in", "punch_out")
LOAD_QUEUE_MAX_SEC = 60.0
# Семпл може ще їхати filesync-ом (перескан раз на 10 с), та й браузер Live
# помічає новий файл не миттєво. Тож чекаємо довше, ніж на девайс.
SAMPLE_QUEUE_MAX_SEC = 180.0
# Локатор чекає на паузу: ставити його -- це рухати плейхед.
CUE_QUEUE_MAX_SEC = 300.0
# Стеля на набір warp-маркерів: подія має лишатись подією, а не файлом.
WARP_MARKERS_MAX = 512
# add_warp_marker бере лише справжній TWarpMarker. Виміряно на живому
# 12.4.3: словник відкинуто, іменований кортеж із тими самими полями --
# теж ("No registered converter ... from this Python object of type
# WarpMarker"). Поля наявного маркера до того ж read-only. Єдиний спосіб
# зробити свій -- узяти клас із маркера, який у кліпі вже є; тому warp
# синхронізується тільки для кліпів, де хоч один маркер існує. Для warp-
# кліпа це завжди так: Live тримає щонайменше один.
# Мікшер ланцюга рака: у Drum Rack кожен пад -- це ланцюг, тож його
# гучність і панорама -- частина звучання, а не оздоблення.
CHAIN_MIX_PARAMS = ("volume", "panning")
CHAIN_TOGGLES = ("mute", "solo")

NOTE_TIME_SPAN = 4.0
NOTE_PITCH_SPAN = 16
NOTE_FIELDS = (
    "pitch", "start_time", "duration", "velocity", "mute",
    "probability", "velocity_deviation", "release_velocity",
)

# Ключі для set_data/get_data -- зберігання всередині самого .als.
# Пріоритет за DATA_KEY_OBJ: uuid лежить на самому об'єкті, тож переживає
# переставляння треків між сесіями. DATA_KEY_MAP -- фолбек однією мапою на Song,
# якщо об'єкти не підтримують set_data; він прив'язаний до позицій і слабший.
DATA_KEY_OBJ = "abletonmp_id"
DATA_KEY_MAP = "abletonmp_registry"


def _log_path():
    # На macOS TMPDIR -- це приватна тека процесу виду /var/folders/xx/.../T,
    # яку система вичищає і яку неможливо назвати наперед. Лог там ніби й є,
    # але знайти його не може ні людина, ні жива проба: warn про warp-маркери
    # ми через це шукали вручну по всьому диску. Кладемо в передбачуване місце.
    base = os.environ.get("APPDATA")
    if not base:
        home = os.path.expanduser("~")
        mac_logs = os.path.join(home, "Library", "Logs")
        base = mac_logs if os.path.isdir(mac_logs) else (
            os.environ.get("XDG_STATE_HOME")
            or os.environ.get("TMPDIR") or home or ".")
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
            "loop": {}, "device_tree": {}, "drum_pads": {}, "song": {},
            "scene_timing": {}, "clip_props": {}, "cues": None, "returns": None,
            "warp": {}, "chain": {}, "stopbtn": {}, "sample": {}, "devstate": {},
        }
        self._obj_cbs = []  # (об'єкт, назва властивості, callback)
        # Перепідписку не можна робити всередині callback-а слота: вона знімає
        # ВСІ listener-и, і сусідня подія, яку Live ще не встиг доставити,
        # помирає разом зі своєю підпискою. Тому -- прапорець і тік.
        self._rewire_pending = False
        # (трек, ланцюг, девайс) -> скільки разів подія не знайшла цілі.
        # Потрібне саме для того, щоб НЕ повторювати те саме попередження.
        self._missing_chain_warned = {}
        self._pending = {}   # key -> відкладена подія, схлопується за ключем
        self._note_pending = {}  # clip key -> {track, scene, clip, due, first}
        self._rec_pending = {}   # clip key -> кліп, що зараз пишеться
        self._load_queue = []    # DeviceLoad: по одному за тік
        self._browser_cache = None
        self._clip_buf = {}  # track_idx -> psi, накопичується між тіками
        self._unshared_tracks = set()  # групи: uuid є, але в мережу не йдуть
        self._group_warned = set()
        self._exc_seen = set()   # про кожне місце -- один рядок партнеру
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
        self._arr_reg = Registry(self._log)   # кліпи в Arrangement
        self._saved_aux_track_records = []
        self._aux_track_records = []
        self._saved_chain_records = []
        self._saved_arr_records = []
        self._arr_records = []
        self._chain_records = []
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
        self._chat = AIChatServer(self._log)
        if self._chat.start():
            self._show_message("AbletonMP AI chat: %s token in %s"
                               % (self._chat.url, self._chat.token_source))

        self._doc.add_is_playing_listener(self._cb_is_playing)
        self._doc.add_tempo_listener(self._cb_tempo)
        self._cb_cues = lambda: self._safe(self._on_cues)
        try:
            self._doc.add_cue_points_listener(self._cb_cues)
        except Exception:
            self._cb_cues = None
        self._song_prop_cbs = {}
        for prop in SONG_PROPS:
            cb = self._make_song_prop_cb(prop)
            try:
                getattr(self._doc, "add_%s_listener" % prop)(cb)
                self._song_prop_cbs[prop] = cb
            except Exception:
                pass  # властивості немає в цій збірці Live
        self._doc.add_tracks_listener(self._cb_tracks)
        self._doc.add_scenes_listener(self._cb_scenes)
        # поява/зникнення return-треку змінює кількість send-ів на кожному треку,
        # а це окремі listener'и -- без цього нові send-и лишились би німими
        self._doc.add_return_tracks_listener(self._cb_tracks)
        self._rewire_tracks()
        self._wire_view()
        self._prime_mirror()

        self._link.send(self._hello_payload())
        self._link.send({"m": "snapshot", "state": self._snapshot()})
        self._log("AbletonMP %s connected, Live %s" % (SCRIPT_VERSION, self._live_version()))
        self._safe(self._probe_persistence)

    def _hello_payload(self):
        """Єдине місце, де збирається hello.

        Копій було дві: одна на старті скрипта, друга на hello_request --
        коли daemon піднявся пізніше за Live. Друга відстала й не несла
        хеша, а на практиці саме вона й доїжджає: Live майже завжди вже
        запущений, коли стартує daemon. Наслідок був тихий і бридкий --
        перевірка версій вважала свіжий скрипт старим і казала про це
        щоразу, тобто попередження, яке треба читати, привчали ігнорувати.
        """
        return {
            "m": "hello",
            "live": self._live_version(),
            "script": SCRIPT_VERSION,
            "pid": os.getpid(),
            "events": APPLY_TYPES,
            "features": FEATURES,
            "sha": self._script_sha(),
        }

    def _probe_persistence(self):
        """Що доступно для зберігання реєстру в цій збірці Live.

        Нічого не пише -- лише дивиться. Друга машина може мати іншу версію Live,
        і тоді цей рядок у лозі одразу пояснює, чому реєстр не пережив сесію.
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
        self._safe(self._flush_notes, True)
        self._safe(self._flush_pending, True)
        self._unwire_tracks()
        self._unwire_view()
        if self._doc is not None:
            for name, cb in (("is_playing", self._cb_is_playing),
                             ("tempo", self._cb_tempo),
                             ("cue_points", self._cb_cues),
                             ("tracks", self._cb_tracks),
                             ("scenes", self._cb_scenes),
                             ("return_tracks", self._cb_tracks)):
                try:
                    if getattr(self._doc, "%s_has_listener" % name)(cb):
                        getattr(self._doc, "remove_%s_listener" % name)(cb)
                except Exception:
                    pass
        for prop, cb in list(getattr(self, "_song_prop_cbs", {}).items()):
            try:
                if getattr(self._doc, "%s_has_listener" % prop)(cb):
                    getattr(self._doc, "remove_%s_listener" % prop)(cb)
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
        """Узагальнена підписка: LOM тримає єдину схему add_/remove_/_has_listener,
        тож перелічувати кожен параметр окремо не треба."""
        if store is None:
            store = self._obj_cbs
        try:
            getattr(obj, "add_%s_listener" % prop)(cb)
            store.append((obj, prop, cb))
        except Exception:
            pass  # параметра тут немає (напр. arm на треку, який не озброюється)

    def _request_rewire(self):
        """Перепідписатись на наступному тіку, а не просто зараз.

        Перетягування кліпа зі слота в слот -- це для Live дві зміни has_clip
        в одному пакеті. Якщо обробник цільового слота перепідпишеться на
        місці, підписка слота-джерела зникне ДО того, як Live її викличе:
        ClipDelete не губиться в дорозі, він не народжується взагалі, і
        партнер лишається з двома кліпами замість одного переїханого.
        """
        self._rewire_pending = True

    def _rewire_tracks(self):
        self._rewire_pending = False
        self._unwire_tracks()
        for track in self._doc.tracks:
            self._listen(track, "playing_slot_index", self._make_slot_cb(track))
            self._wire_metadata("track", track, track=track)
            self._wire_mixer(track)
            self._wire_devices(track)
            self._wire_note_slots(track)
            self._listen(track, "arrangement_clips", self._make_arrangement_cb())
            self._wire_arrangement_clips(track)
        for track in self._device_aux_tracks():
            self._wire_metadata("track", track, track=track)
            self._wire_mixer(track)
            self._wire_devices(track)
        for scene in self._doc.scenes:
            self._wire_metadata("scene", scene, scene=scene)
            for prop in ("tempo", "tempo_enabled", "time_signature_numerator",
                         "time_signature_denominator", "time_signature_enabled"):
                self._listen(scene, prop, self._make_scene_timing_cb(scene))

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
            # Стоп-кнопка живе на СЛОТІ, а не на кліпі: вона є й у порожнього,
            # і саме порожній слот зі стоп-кнопкою зупиняє трек на SceneLaunch.
            self._listen(slot, "has_stop_button",
                         self._make_stop_button_cb(track, scene, slot))
            try:
                if slot.has_clip:
                    clip = slot.clip
                    self._wire_metadata("clip", clip, track=track, scene=scene)
                    loop_cb = self._make_clip_loop_cb(track, scene, clip)
                    for prop in CLIP_LOOP_PROPS:
                        self._listen(clip, prop, loop_cb)
                    for prop in CLIP_PROPS:
                        self._listen(clip, prop,
                                     self._make_clip_prop_cb(track, scene, clip, prop))
                    self._listen(clip, "warp_markers",
                                 self._make_warp_cb(track, scene, clip))
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
        # crossfade_assign -- звичайна int-властивість mixer_device, а не
        # DeviceParameter, тож у цикл параметрів вище вона не потрапляє.
        if self._aux_kind_of(track) != "master":
            md = self._safe_attr(track, "mixer_device")
            if md is not None:
                self._listen(md, "crossfade_assign",
                             self._make_mix_cb(track, "crossfade_assign", None))

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
            # Маркери на хвилі Simpler -- не параметри, у них власний обʼєкт.
            # _listen мовчки ковтає властивість без listener, тож девайси без
            # семпла тут нічого не коштують.
            sample = self._sample_of(device)
            if sample is not None:
                for prop in SAMPLE_PROPS:
                    self._listen(sample, prop,
                                 self._make_sample_prop_cb(track, device, prop))
            # Стан повз parameters. hasattr замість таблиці класів: девайс,
            # у якого властивості немає, просто не потрапляє в підписку.
            for prop in self._device_state_props(device):
                self._listen(device, prop,
                             self._make_device_state_cb(track, device, prop))
            if not self._device_has_chains(device):
                continue
            for kind, chains in self._rack_chain_groups(device):
                self._listen(device, kind, self._make_devices_cb())
                for chain in chains:
                    cid = self._chains_reg.id_of(chain, create=False)
                    if cid:
                        self._wire_chain_mixer(chain, cid)
                        self._wire_metadata("chain", chain)
                    self._wire_device_container(track, chain, depth + 1)

    def _unwire_tracks(self):
        for obj, prop, cb in self._obj_cbs:
            try:
                if getattr(obj, "%s_has_listener" % prop)(cb):
                    getattr(obj, "remove_%s_listener" % prop)(cb)
            except Exception:
                pass  # об'єкт уже видалений -- звертання кидає RuntimeError
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

    def _make_arrangement_cb(self):
        def cb():
            self._safe(self._on_arrangement)
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
            return  # це відлуння нашого власного apply
        self._mirror["playing"] = playing
        self._emit("TransportSet", {"playing": playing})

    def _on_tempo(self):
        """Темп змінився.

        Поріг, а не точна рівність, і на це є виміряна причина. З увімкненим
        Ableton Link темп доїжджає до партнера ще й через сам Link, але з
        мікроскопічною похибкою: 137 стає 137.000061. Наш listener бачив це
        як нову зміну й емітив у відповідь, тобто кожен рух темпу давав
        зайву пару подій і повільно розганяв значення.

        Тисячна доля BPM не чутна нікому. Усе, що менше, -- це не зміна
        темпу, а шум від чужого клоку.
        """
        bpm = round(float(self._doc.tempo), 6)
        previous = self._mirror["tempo"]
        if previous is not None and abs(previous - bpm) < TEMPO_EPSILON:
            return
        self._mirror["tempo"] = bpm
        self._defer("tempo", "TempoSet", {"bpm": bpm})

    def _song_prop_value(self, prop, raw):
        """Нормалізоване значення властивості пісні, або None.

        Валідація тут, а не в місці застосування: та сама перевірка потрібна
        і при емісії (щоб не слати сміття), і при прийомі (щоб не записати
        чуже сміття в LOM).
        """
        try:
            if prop == "scale_name":
                text = self._doc_str(raw)
                return text if text and len(text) <= 64 else None
            if prop in ("scale_mode", "loop", "punch_in", "punch_out"):
                return bool(raw)
            if prop in ("loop_start", "loop_length"):
                value = round(float(raw), 6)
                if not math.isfinite(value) or value < 0 or value > CLIP_LENGTH_MAX:
                    return None
                # Нульова довжина петлі -- не петля, а Live її мовчки
                # підтягне до мінімуму, і партнери розійдуться.
                if prop == "loop_length" and value <= 0:
                    return None
                return value
            value = int(raw)
        except Exception:
            return None
        if prop == "signature_numerator":
            return value if 1 <= value <= 99 else None
        if prop == "signature_denominator":
            # Live приймає лише степені двійки; інше він мовчки округлить,
            # і партнери розійдуться, не помітивши.
            return value if value in (1, 2, 4, 8, 16) else None
        if prop == "clip_trigger_quantization":
            return value if 0 <= value <= 13 else None
        if prop == "root_note":
            return value if 0 <= value <= 11 else None
        return None

    def _prime_song_props(self):
        state = {}
        for prop in SONG_PROPS:
            value = self._song_prop_value(prop, self._safe_attr(self._doc, prop))
            if value is not None:
                state[prop] = value
        self._mirror["song"] = state

    def _make_song_prop_cb(self, prop):
        def cb():
            self._safe(self._on_song_prop, prop)
        return cb

    def _on_song_prop(self, prop):
        value = self._song_prop_value(prop, self._safe_attr(self._doc, prop))
        if value is None:
            return
        if self._mirror["song"].get(prop) == value:
            return
        self._mirror["song"][prop] = value
        # Дебаунс спільний із темпом: розмір такту й квантизацію теж тягнуть
        # мишею, і один жест має дати одну подію.
        self._defer("song:%s" % prop, "SongPropSet", {"prop": prop, "value": value})

    def _apply_song_prop(self, payload, gseq):
        prop = payload.get("prop")
        if prop not in SONG_PROPS:
            self._warn("gseq %s: невідома властивість пісні %r" % (gseq, prop))
            return
        value = self._song_prop_value(prop, payload.get("value"))
        if value is None:
            self._warn("gseq %s: некоректне значення %r для %s"
                       % (gseq, payload.get("value"), prop))
            return
        self._mirror["song"][prop] = value   # ДО запису в LOM -- глушимо ехо
        try:
            setattr(self._doc, prop, value)
        except Exception as e:
            self._warn("gseq %s: %s не встановився: %r" % (gseq, prop, e))
        # Live міг клампнути або відхилити -- перечитуємо, як в ObjectMetaSet
        actual = self._song_prop_value(prop, self._safe_attr(self._doc, prop))
        if actual is not None:
            self._mirror["song"][prop] = actual

    # ---------------------------------------------------- темп і метр сцени
    #
    # Live віддає -1 замість значення, коли перевизначення вимкнене, тож поля
    # взаємозалежні: писати tempo при вимкненому tempo_enabled безглуздо.
    # Звідси й форма -- увесь блок однією подією, як у ClipLoopSet.

    def _scene_timing(self, scene):
        """Блок перевизначень сцени, або None, якщо їх у цій збірці немає."""
        enabled = self._safe_attr(scene, "tempo_enabled")
        sig_enabled = self._safe_attr(scene, "time_signature_enabled")
        if enabled is None and sig_enabled is None:
            return None
        block = {"tempo_enabled": bool(enabled),
                 "time_signature_enabled": bool(sig_enabled)}
        if block["tempo_enabled"]:
            try:
                tempo = round(float(scene.tempo), 6)
            except Exception:
                tempo = None
            if tempo is not None and 20.0 <= tempo <= 999.0:
                block["tempo"] = tempo
        if block["time_signature_enabled"]:
            for prop, key in (("time_signature_numerator", "numerator"),
                              ("time_signature_denominator", "denominator")):
                try:
                    value = int(getattr(scene, prop))
                except Exception:
                    continue
                if key == "numerator" and 1 <= value <= 99:
                    block[prop] = value
                elif key == "denominator" and value in (1, 2, 4, 8, 16):
                    block[prop] = value
        return block

    def _prime_scene_timing(self):
        state = {}
        for scene in self._doc.scenes:
            sid = self._scenes_reg.id_of(scene, create=False)
            if not sid:
                continue
            block = self._scene_timing(scene)
            if block is not None:
                state[sid] = block
        self._mirror["scene_timing"] = state

    def _make_scene_timing_cb(self, scene):
        def cb():
            self._safe(self._on_scene_timing, scene)
        return cb

    def _on_scene_timing(self, scene):
        if not self._registry_ready or self._suppress_struct:
            return
        sid = self._scenes_reg.id_of(scene, create=False)
        if not sid:
            return
        block = self._scene_timing(scene)
        if block is None or self._mirror["scene_timing"].get(sid) == block:
            return
        self._mirror["scene_timing"][sid] = block
        payload = {"scene": {"id": sid}}
        payload.update(block)
        # Спільний ключ на всі пʼять полів: увімкнути перевизначення й виставити
        # значення -- це один жест користувача, а не пʼять подій.
        self._defer("scene_timing:%s" % sid, "SceneTimingSet", payload)

    def _apply_scene_timing(self, payload, gseq):
        sidx = self._resolve_scene(payload.get("scene"))
        if sidx is None:
            return
        try:
            scene = list(self._doc.scenes)[sidx]
        except Exception:
            return
        sid = (payload.get("scene") or {}).get("id")

        # Спершу повна валідація, і лише потім записи: часткове застосування
        # лишило б сцену з увімкненим перевизначенням і чужим значенням.
        want = {"tempo_enabled": bool(payload.get("tempo_enabled")),
                "time_signature_enabled": bool(payload.get("time_signature_enabled"))}
        if want["tempo_enabled"]:
            try:
                tempo = round(float(payload.get("tempo")), 6)
            except Exception:
                tempo = None
            if tempo is None or not (20.0 <= tempo <= 999.0):
                self._warn("gseq %s: некоректний темп сцени %r" % (gseq, payload.get("tempo")))
                return
            want["tempo"] = tempo
        if want["time_signature_enabled"]:
            try:
                num = int(payload.get("time_signature_numerator"))
                den = int(payload.get("time_signature_denominator"))
            except Exception:
                self._warn("gseq %s: некоректний метр сцени" % (gseq,))
                return
            if not (1 <= num <= 99) or den not in (1, 2, 4, 8, 16):
                self._warn("gseq %s: метр сцени %r/%r поза межами" % (gseq, num, den))
                return
            want["time_signature_numerator"] = num
            want["time_signature_denominator"] = den

        if sid:
            self._mirror["scene_timing"][sid] = want  # ДО запису -- глушимо ехо
        try:
            # Порядок незвертальний: доки перевизначення вимкнене, Live віддає
            # -1 і значення просто нікуди писати.
            scene.tempo_enabled = want["tempo_enabled"]
            if want["tempo_enabled"]:
                scene.tempo = want["tempo"]
            scene.time_signature_enabled = want["time_signature_enabled"]
            if want["time_signature_enabled"]:
                scene.time_signature_numerator = want["time_signature_numerator"]
                scene.time_signature_denominator = want["time_signature_denominator"]
        except Exception as e:
            self._warn("gseq %s: темп/метр сцени не встановились: %r" % (gseq, e))
        if sid:
            actual = self._scene_timing(scene)
            if actual is not None:
                self._mirror["scene_timing"][sid] = actual

    def _on_tracks(self):
        # структура треків змінилась: перепідписуємось і скидаємо дзеркало слотів,
        # інакше зсув індексів породить фантомні ClipLaunch
        self._flush_notes(True)
        self._rewire_tracks()
        self._mirror["psi"] = {}
        self._clip_buf = {}  # накопичене посилається на старі індекси
        self._prime_mirror(transport=False)
        if self._registry_ready:
            self._diff_tracks(emit=not self._suppress_struct)
            aux_changed = self._refresh_aux_tracks()
            if not self._suppress_struct:
                self._safe(self._diff_returns)
            if self._refresh_chains() or aux_changed:
                self._persist_registry()
            self._prime_mixer()  # listener'и мікшера перевішані на нові об'єкти
            self._prime_devices()
            self._prime_samples()
            self._prime_device_state()
            self._prime_notes()
            self._prime_metadata()
            self._prime_clip_loops()
            self._prime_stop_buttons()
            self._prime_all_clip_props()
            self._prime_all_clip_warp()
            self._prime_arrangement_clips()
            self._prime_chains_mix()
            # Набір треків змінився -- разом із ним і набір лінійок, які ми
            # обходимо. Без цього запис про кліп на видаленому треку висів би
            # у мапі до наступної зміни в Arrangement.
            self._prime_arrangement()

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
            self._prime_samples()
            self._prime_device_state()
            self._prime_notes()
            self._prime_metadata()
            self._prime_clip_loops()
            self._prime_stop_buttons()
            self._prime_all_clip_props()
            self._prime_all_clip_warp()
            self._prime_arrangement_clips()
            self._prime_chains_mix()
            # Набір треків змінився -- разом із ним і набір лінійок, які ми
            # обходимо. Без цього запис про кліп на видаленому треку висів би
            # у мапі до наступної зміни в Arrangement.
            self._prime_arrangement()

            # Виділення могло переїхати разом зі структурою: адреса в підписі
            # уже інша, навіть якщо користувач нічого не чіпав.
            self._touch_view()

    def _on_devices(self):
        """Rebind observers after any Track/Chain/Rack structure change."""
        changed = self._refresh_chains()
        self._rewire_tracks()
        if self._registry_ready:
            if not self._suppress_struct:
                self._safe(self._diff_devices)
                # Окремо від _diff_devices: семпл на паді народжує новий
                # ланцюг, а нові контейнери дифф девайсів пропускає.
                self._safe(self._diff_drum_pads)
            # Значення параметрів після структурної зміни -- нова базова лінія,
            # інакше поява девайса дала б залп DeviceParamSet на кожну ручку.
            self._prime_devices()
            self._prime_samples()
            self._prime_device_state()
            if changed:
                self._persist_registry()

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
        self._aux_tracks_reg.clear()
        self._chains_reg.clear()
        self._arr_reg.clear()
        # спершу піднімаємо uuid із самого .als: якщо обидві машини відкрили той
        # самий файл, вони отримають однакові uuid ще до будь-якого обміну
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
        self._prime_samples()
        self._prime_device_state()
        self._prime_notes()
        self._prime_metadata()
        self._prime_clip_loops()
        self._prime_stop_buttons()
        self._prime_all_clip_props()
        self._prime_all_clip_warp()
        self._prime_arrangement_clips()
        self._prime_chains_mix()
        self._prime_song_props()
        self._prime_scene_timing()
        self._prime_all_clip_props()
        self._prime_all_clip_warp()
        self._prime_arrangement_clips()
        self._prime_chains_mix()
        self._prime_cues()
        self._prime_returns()
        self._prime_arrangement()
        self._persist_registry()
        self._log("registry created: %d tracks, %d scenes, %d aux tracks, %d Rack chains (%d ids restored)"
                  % (len(reg["tracks"]), len(reg["scenes"]), len(reg["aux_tracks"]),
                     len(reg["chains"]), restored))
        return reg

    def _adopt_registry(self, reg):
        """Накладає чужі uuid на свої об'єкти за позицією, звіряючи імена.

        Це єдине місце, де індекс ще є адресою -- одноразово, на бутстрапі.
        Далі індекси в протоколі не фігурують взагалі.
        """
        self._tracks_reg.clear()
        self._scenes_reg.clear()
        self._aux_tracks_reg.clear()
        self._chains_reg.clear()
        self._arr_reg.clear()
        # uuid, збережені в .als, головніші за позицію: ім'я треку в Live
        # змінюється саме собою від кинутого девайса, тож звірка за іменем
        # відкидала б цілком легітимні збіги
        self._restore_registry()
        problems = []
        by_data = 0
        by_position = 0
        matched = {}  # за видом: скільки записів було і скільки зійшлось

        for kind, records, objects, reg_obj in (
            ("трек", reg.get("tracks") or [], self._doc.tracks, self._tracks_reg),
            ("сцена", reg.get("scenes") or [], self._doc.scenes, self._scenes_reg),
        ):
            matched[kind] = [len(records), 0]
            for rec in records:
                uid = rec.get("id")
                if uid and reg_obj.obj_of(uid) is not None:
                    by_data += 1
                    matched[kind][1] += 1
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
        self._prime_samples()
        self._prime_device_state()
        self._prime_notes()
        self._prime_metadata()
        self._prime_clip_loops()
        self._prime_stop_buttons()
        self._prime_all_clip_props()
        self._prime_all_clip_warp()
        self._prime_arrangement_clips()
        self._prime_chains_mix()
        self._prime_song_props()
        self._prime_scene_timing()
        self._prime_all_clip_props()
        self._prime_all_clip_warp()
        self._prime_arrangement_clips()
        self._prime_chains_mix()
        self._prime_cues()
        self._prime_returns()
        self._prime_arrangement()
        # канонічні uuid із журналу лягають у .als, щоб наступного разу проєкт
        # відкрився вже з ними і бутстрап за позиціями не знадобився
        self._persist_registry()
        self._log("registry adopted: %d tracks, %d scenes, %d aux tracks, %d Rack chains "
                  "(%d stored, %d by position)"
                  % (len(self._tracks_reg), len(self._scenes_reg), len(self._aux_track_records),
                     len(self._chain_records), by_data, by_position))
        if problems:
            # проєкти розійшлись; події на незіставлені об'єкти просто не застосуються
            self._warn("бутстрап реєстру, незіставлено %d: %s"
                       % (len(problems), "; ".join(problems[:5])))
        # Рахуємо окремо по видах: сцени часто зіставляються навіть у чужому
        # проєкті (їх адресує позиція в мапі), і сумарний лічильник це маскував би.
        # Нуль треків -- це повна німота: без uuid трека жодна подія мікшера,
        # кліпа чи структури не має адреси.
        for kind, (total, ok) in matched.items():
            if total and not ok:
                self._warn("ЖОДЕН %s не зіставився (%d у сесії) -- цей проєкт не той, "
                           "що в сесії relay. Події по %sх працювати не будуть; "
                           "відкрий той самий .als або заведи нову --session"
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
        """Піднімає uuid, збережені в .als. Повертає кількість відновлених.

        Обидва механізми працюють разом, не замість одного: у Live 12 трек тримає
        set_data, а сцена -- ні, тож частина об'єктів впізнає себе сама, а решту
        доводиться діставати з мапи на Song. Ранній вихід після першого проходу
        залишав би сцени без ідентичності назавжди.
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
        self._saved_arr_records = list((saved or {}).get("arrangement") or [])
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
                        continue  # об'єкт уже впізнав себе через set_data
                    if rec.get("name") and self._safe_name(objects[i]) != rec["name"]:
                        continue
                    reg.bind(rec.get("id"), objects[i])
                    by_map += 1

            # Другий прохід -- за назвою, і тільки для сцен. Сцена не тримає
            # set_data, тож її ідентичність живе виключно в мапі за індексом:
            # варто переставити сцени між сесіями, і за тим індексом уже інша
            # сцена. Перевірка імені чесно відмовляє -- і uuid губиться назовсім.
            #
            # Якщо ж назва унікальна з обох боків, вона сама по собі адреса,
            # незалежна від порядку. Неунікальну назву (і порожню, яку Live
            # дає за замовчуванням) не чіпаємо: краще втратити ідентичність,
            # ніж привʼязати uuid до не тієї сцени.
            saved_by_name = {}
            for rec in (saved.get("scenes") or []):
                name = rec.get("name")
                if name and rec.get("id"):
                    saved_by_name.setdefault(name, []).append(rec)
            live_by_name = {}
            for obj in self._doc.scenes:
                name = self._safe_name(obj)
                if name:
                    live_by_name.setdefault(name, []).append(obj)
            for name, recs in saved_by_name.items():
                live = live_by_name.get(name) or []
                if len(recs) != 1 or len(live) != 1:
                    continue
                obj = live[0]
                uid = recs[0]["id"]
                if self._scenes_reg.id_of(obj, create=False):
                    continue
                if self._scenes_reg.taken_by_other(uid, obj):
                    continue
                self._scenes_reg.bind(uid, obj)
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
                "chains": list(self._chain_records),
                "arrangement": list(self._arr_records)}
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
        """Звіряє реєстр із деревом треків після зміни структури."""
        created, removed = self._tracks_reg.diff(self._doc.tracks)
        # Ctrl+D копіює трек РАЗОМ із set_data, тож копія приходить із
        # ідентифікатором джерела. Це і є детектор дублювання -- точний
        # і пасивний. Читати треба ДО _persist_registry, який перезапише
        # успадкований id на щойно виданий власний.
        duplicated = {}
        for uid, _idx, track in created:
            stored = self._obj_stored_id(track)
            if not stored or stored == uid:
                continue
            source = self._tracks_reg.obj_of(stored)
            if source is not None:
                duplicated[uid] = stored
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
            source = duplicated.get(uid)
            if source:
                # Партнер повторить дію, а не отримає вміст: у нього те саме
                # джерело, тож копія вийде з девайсами й семплами.
                self._emit("TrackDuplicate", {
                    "source": {"id": source},
                    "track": ref,
                    "idx": idx,
                    "kind": self._track_kind(track),
                })
                continue
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
        if kind == "chain":
            uid = self._chains_reg.id_of(obj, create=False)
            return {"object": kind, "chain": {"id": uid}} if uid else None
        if kind == "clip" and track is not None and scene is not None:
            refs = self._clip_refs(track, scene)
            if refs["track"].get("id") and refs["scene"].get("id"):
                refs["object"] = kind
                return refs
        if kind == "clip" and obj is not None:
            # Кліп у лінійці: сцени немає, зате є власний uuid
            uid = self._arr_reg.id_of(obj, create=False)
            if uid:
                ref = self._arr_track_ref(obj)
                if ref:
                    return {"object": kind, "track": ref, "clip": {"id": uid}}
        return None

    def _arr_track_ref(self, clip):
        """Трек, якому належить Arrangement-кліп."""
        for track in self._doc.tracks:
            try:
                for candidate in self._arr_clips(track):
                    if candidate == clip:
                        return self._device_track_ref(track)
            except Exception:
                continue
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

    def _send_return_ref(self, idx):
        """uuid Return-треку за позицією сенда, або None."""
        if not isinstance(idx, int) or idx < 0:
            return None
        try:
            returns = list(self._doc.return_tracks)
        except Exception:
            return None
        if idx >= len(returns):
            return None
        rid = self._aux_tracks_reg.id_of(returns[idx], create=False)
        return {"id": rid} if rid else None

    @staticmethod
    def _mix_track_key(track_ref):
        if not isinstance(track_ref, dict):
            return str(track_ref)
        kind = track_ref.get("kind")
        uid = track_ref.get("id")
        return "%s:%s" % (kind, uid) if kind else str(uid)

    def _crossfade_assign(self, track):
        """0 = A, 1 = none, 2 = B. None -- у цього треку його немає."""
        md = self._safe_attr(track, "mixer_device")
        if md is None:
            return None
        try:
            value = int(md.crossfade_assign)
        except Exception:
            return None
        return value if value in (0, 1, 2) else None

    def _mix_key(self, track_ref, param, idx):
        return "%s:%s:%s" % (self._mix_track_key(track_ref), param, idx)

    def _toggle_key(self, track_ref, prop):
        return "%s:%s" % (self._mix_track_key(track_ref), prop)

    def _on_mix(self, track, param, idx):
        if not self._registry_ready:
            return
        track_ref = self._device_track_ref(track)
        if param == "crossfade_assign":
            value = self._crossfade_assign(track)
            key = self._mix_key(track_ref, param, idx)
            if not track_ref or value is None or self._mirror["mix"].get(key) == value:
                return
            self._mirror["mix"][key] = value
            # дискретний перемикач A/none/B -- дебаунс лише додав би затримки
            self._emit("MixerSet", {"track": track_ref, "param": param, "value": value})
            return
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
        if param == "send":
            # Індекс сенда -- позиція, а позиція між машинами не збігається,
            # щойно в когось інша кількість чи інший порядок Return-треків.
            # Тому поруч їде uuid цільового Return: не адреса, а контрольна
            # сума. Краще гучна відмова, ніж сенд, що поїхав не в той ревер.
            ret = self._send_return_ref(idx)
            if ret:
                payload["return"] = ret
        # неперервна величина -- дебаунсимо, як tempo: рух фейдера це один жест
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
        # дискретне перемикання -- дебаунс тут лише додав би затримки
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

    def _rack_chain_slots(self, device):
        """(kind, адреса, ланцюг) для кожного ланцюга раку.

        Для Drum Rack адреса -- НОТА пада, а не позиція в device.chains.
        Причина виміряна на живій парі: device.chains містить лише заповнені
        пади, тож один зайвий пад у партнера зсуває всі наступні індекси, і
        разом з ними злітає ідентичність усіх ланцюгів після нього. Нота ж
        означає те саме на обох машинах незалежно від вмісту.

        Назву пада в адресу теж не беремо: це назва семплу, тобто рівно те,
        що ми синхронізуємо. Адресувати об'єкт тим, що змінюється, -- це
        гарантований розсинхрон при першій же заміні семплу.
        """
        pads = []
        try:
            pads = list(device.drum_pads)
        except Exception:
            pads = []
        if not pads:
            out = []
            for kind, chains in self._rack_chain_groups(device):
                for idx, chain in enumerate(chains):
                    out.append((kind, {"idx": idx,
                                       "name": self._safe_name(chain)}, chain))
            return out

        out = []
        for pad in pads:
            note = self._safe_attr(pad, "note")
            if note is None:
                continue
            try:
                chains = list(pad.chains)
            except Exception:
                continue
            for sub_idx, chain in enumerate(chains):
                out.append(("chains", {"note": int(note), "sub": sub_idx}, chain))
        try:
            for idx, chain in enumerate(device.return_chains):
                out.append(("return_chains", {"idx": idx,
                                              "name": self._safe_name(chain)}, chain))
        except Exception:
            pass
        return out

    # Поля адреси -- рівно ті, що можуть бути в локаторі. Один перелік на
    # побудову і на розбір запису: розійдуться -- і збережений реєстр тихо
    # перестане зіставлятись сам із собою.
    CHAIN_LOCATOR_FIELDS = ("track", "parent_chain", "rack", "kind",
                            "idx", "name", "note", "sub", "track_kind")

    def _chain_locator(self, track_ref, parent_id, container, rack, kind, addr, chain):
        locator = {
            "track": track_ref.get("id"),
            "parent_chain": parent_id,
            "rack": self._device_ref(container, rack),
            "kind": kind,
        }
        locator.update(addr)
        if track_ref.get("kind"):
            locator["track_kind"] = track_ref["kind"]
        return locator

    @classmethod
    def _chain_locator_of_record(cls, rec):
        """Локатор із збереженого запису -- рівно ті поля, що в ньому є."""
        return dict((k, rec[k]) for k in cls.CHAIN_LOCATOR_FIELDS if k in rec)

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
            if rec.get("id"):
                key = self._chain_locator_key(self._chain_locator_of_record(rec))
                preferred[key] = rec["id"]
        saved = {}
        for rec in self._saved_chain_records:
            if rec.get("id"):
                key = self._chain_locator_key(self._chain_locator_of_record(rec))
                saved[key] = rec["id"]

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
                for kind, addr, chain in self._rack_chain_slots(rack):
                    locator = self._chain_locator(
                        track_ref, parent_id, container, rack, kind, addr, chain)
                    if locator["rack"] is None:
                        continue
                    locator_key = self._chain_locator_key(locator)
                    uid = self._chains_reg.id_of(chain, create=False)
                    if uid is None:
                        # Порядок тут -- це вибір між "моя правда" і "спільна
                        # правда", і спільна має вигравати. Канонічний запис
                        # від партнера -- перший. Далі ДЕТЕРМІНОВАНИЙ хеш від
                        # локатора: обидві машини рахують його однаково, тож
                        # ланцюг, якого партнер ніколи не бачив, усе одно
                        # дістане в нас той самий uuid, що й у нього.
                        #
                        # Збережений id стоїть НИЖЧЕ хеша навмисно. Раніше він
                        # був вищий -- і кожна машина трималась за uuid, який
                        # намінтила сама; зійтись вони не могли вже ніколи.
                        # На живій парі це давало 53 "device at chain path is
                        # absent" за один прогін: параметри всередині раків
                        # не їхали взагалі.
                        by_hash = hashlib.sha256(
                            locator_key.encode("utf-8")).hexdigest()[:12]
                        uid = self._free_id(
                            self._chains_reg, chain,
                            preferred.get(locator_key),
                            by_hash,
                            self._obj_stored_id(chain),
                            saved.get(locator_key))
                        if not uid:
                            uid = by_hash
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

    def _resolve_device_only(self, track, chain_path, device_ref):
        """Девайс без параметра. Той самий шлях адресації, що й у
        _resolve_device_parameter -- просто зупиняємось на девайсі."""
        if track is None or not isinstance(device_ref, dict):
            return None
        probe = {"name": "", "ordinal": 0}
        device, _parameter = self._resolve_device_parameter(
            track, chain_path, device_ref, probe)
        return device

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

    @staticmethod
    def _sample_of(device):
        """Обʼєкт Sample девайса, або None. Є лише в Simpler/Sampler."""
        try:
            sample = device.sample
        except Exception:
            return None
        return sample

    @staticmethod
    def _device_state_props(device):
        """Які з відомих властивостей стану є саме в цього девайса."""
        found = []
        for prop in DEVICE_STATE_PROPS:
            try:
                value = getattr(device, prop)
            except Exception:
                continue
            if value is None or isinstance(value, (bool, int, float)):
                found.append(prop)
        return found

    def _make_device_state_cb(self, track, device, prop):
        def cb():
            self._safe(self._on_device_state, track, device, prop)
        return cb

    @staticmethod
    def _device_state_value(device, prop):
        try:
            value = getattr(device, prop)
        except Exception:
            return None
        if isinstance(value, bool):
            return value
        try:
            value = float(value)
        except Exception:
            return None
        if not math.isfinite(value):
            return None
        return int(value) if float(value).is_integer() else round(value, 6)

    def _on_device_state(self, track, device, prop):
        if not self._registry_ready or self._suppress_struct:
            return
        track_ref = self._device_track_ref(track)
        _container, device_ref, chain_path = self._device_location(track, device)
        if not track_ref or device_ref is None or chain_path is None:
            return
        value = self._device_state_value(device, prop)
        if value is None:
            return
        key = self._sample_key(track_ref, chain_path, device_ref, prop)
        if self._mirror["devstate"].get(key) == value:
            return
        self._mirror["devstate"][key] = value
        payload = {"track": track_ref, "device": device_ref,
                   "prop": prop, "value": value}
        if chain_path:
            payload["chain_path"] = chain_path
        # Частина цих властивостей -- перемикачі, частина крутиться мишею
        # (ir_decay_time, gain). Дебаунс безпечний для обох.
        self._defer("devstate:" + key, "DeviceStateSet", payload)

    def _prime_device_state(self):
        self._mirror["devstate"] = {}
        for track in self._iter_device_tracks():
            track_ref = self._device_track_ref(track)
            if not track_ref:
                continue
            for container, device, chain_path in self._iter_track_devices(track):
                device_ref = self._device_ref(container, device)
                if device_ref is None:
                    continue
                for prop in self._device_state_props(device):
                    value = self._device_state_value(device, prop)
                    if value is not None:
                        key = self._sample_key(track_ref, chain_path, device_ref, prop)
                        self._mirror["devstate"][key] = value

    def _device_state_block(self, device):
        """Стан девайса для знімка. None -- нічого понад parameters."""
        state = {}
        for prop in self._device_state_props(device):
            value = self._device_state_value(device, prop)
            if value is not None:
                state[prop] = value
        return state or None

    def _make_sample_prop_cb(self, track, device, prop):
        def cb():
            self._safe(self._on_sample_prop, track, device, prop)
        return cb

    def _sample_prop_value(self, sample, prop):
        try:
            value = getattr(sample, prop)
        except Exception:
            return None
        if prop in SAMPLE_BOOL_PROPS:
            return bool(value)
        try:
            value = float(value)
        except Exception:
            return None
        if not math.isfinite(value) or value < 0:
            return None
        return int(value) if prop in SAMPLE_INT_PROPS else round(value, 6)

    def _sample_key(self, track_ref, chain_path, device_ref, prop):
        return "%s|%s|%s#%s|%s" % (
            (track_ref or {}).get("id"),
            "/".join((c or {}).get("id") or "?" for c in (chain_path or [])),
            (device_ref or {}).get("class_name"), (device_ref or {}).get("ordinal"),
            prop)

    def _on_sample_prop(self, track, device, prop):
        """Маркер або підсилення семплу зрушено.

        Дебаунс тут не з економії: маркер тягнуть мишею, і жест дає десятки
        значень. Одна дія -- одна подія.
        """
        if not self._registry_ready or self._suppress_struct:
            return
        sample = self._sample_of(device)
        if sample is None:
            return
        track_ref = self._device_track_ref(track)
        _container, device_ref, chain_path = self._device_location(track, device)
        if not track_ref or device_ref is None or chain_path is None:
            return
        value = self._sample_prop_value(sample, prop)
        if value is None:
            return
        key = self._sample_key(track_ref, chain_path, device_ref, prop)
        if self._mirror["sample"].get(key) == value:
            return
        self._mirror["sample"][key] = value
        payload = {"track": track_ref, "device": device_ref,
                   "prop": prop, "value": value}
        if chain_path:
            payload["chain_path"] = chain_path
        self._defer("sampleprop:" + key, "SamplePropSet", payload)

    def _prime_samples(self):
        """Без прайму перший погляд на семпл виглядав би як зміна проти None."""
        self._mirror["sample"] = {}
        for track in self._iter_device_tracks():
            track_ref = self._device_track_ref(track)
            if not track_ref:
                continue
            for _container, device, chain_path in self._iter_track_devices(track):
                sample = self._sample_of(device)
                if sample is None:
                    continue
                device_ref = self._device_ref(_container, device)
                if device_ref is None:
                    continue
                for prop in SAMPLE_PROPS:
                    value = self._sample_prop_value(sample, prop)
                    if value is not None:
                        key = self._sample_key(track_ref, chain_path, device_ref, prop)
                        self._mirror["sample"][key] = value

    def _sample_state(self, device):
        """Блок семплу для знімка. None -- девайс його не має."""
        sample = self._sample_of(device)
        if sample is None:
            return None
        state = {}
        for prop in SAMPLE_PROPS:
            value = self._sample_prop_value(sample, prop)
            if value is not None:
                state[prop] = value
        return state or None

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

    def _resolve_any_clip(self, payload, gseq):
        """Кліп за адресою -- сесійний або з лінійки.

        Сесійний адресується парою (трек, сцена), бо власного uuid у нього
        немає. Кліп у лінійці навпаки: сцен там не буває, зате є uuid.
        Одна подія має вміти в обидва, інакше audio-кліп у лінійці лишився
        б без gain і warp -- саме там, де warp найпотрібніший.
        """
        uid = (payload.get("clip") or {}).get("id")
        if uid:
            _track, clip = self._resolve_arr_clip(payload)
            # Ключ дзеркала обовʼязковий: без нього застосування не глушить
            # ехо, listener бачить "чужу" зміну і шле її назад по колу.
            return clip, ("arr:" + uid if clip is not None else None)
        track, scene, slot = self._resolve_clip_slot(payload, gseq)
        if slot is None:
            return None, None
        try:
            if not slot.has_clip:
                return None, None
            return slot.clip, self._clip_key(track, scene)
        except Exception:
            return None, None

    def _clip_prop_value(self, prop, raw):
        """Нормалізоване значення властивості кліпу, або None.

        Одна перевірка на емісію і на прийом: інакше рано чи пізно
        розійдуться, і саме приймальний бік запише в LOM те, чого не буває.
        """
        try:
            if prop in ("warping", "muted", "legato", "ram_mode"):
                return bool(raw)
            if prop == "groove_amount":
                value = round(float(raw), 6)
                return value if math.isfinite(value) and 0.0 <= value <= 1.0 else None
            if prop in ("gain", "velocity_amount"):
                value = round(float(raw), 6)
                if not math.isfinite(value):
                    return None
                return value if 0.0 <= value <= 1.0 else None
            value = int(raw)
        except Exception:
            return None
        if prop == "pitch_coarse":
            return value if -48 <= value <= 48 else None
        if prop == "pitch_fine":
            return value if -50 <= value <= 50 else None
        if prop == "warp_mode":
            return value if 0 <= value <= 6 else None
        if prop == "launch_mode":
            return value if 0 <= value <= 4 else None
        if prop == "launch_quantization":
            return value if 0 <= value <= 13 else None
        if prop == "signature_numerator":
            return value if 1 <= value <= 99 else None
        if prop == "signature_denominator":
            # Live приймає лише степені двійки; інше округлить мовчки
            return value if value in (1, 2, 4, 8, 16) else None
        return None

    def _clip_props_state(self, clip):
        """Знімок властивостей кліпу. Відсутні в цьому типі просто пропускаємо."""
        state = {}
        for prop in CLIP_PROPS:
            value = self._clip_prop_value(prop, self._safe_attr(clip, prop))
            if value is not None:
                state[prop] = value
        return state

    def _make_clip_prop_cb(self, track, scene, clip, prop):
        def cb():
            self._safe(self._on_clip_prop, track, scene, clip, prop)
        return cb

    def _on_clip_prop(self, track, scene, clip, prop):
        if not self._registry_ready or self._suppress_struct:
            return
        key = self._clip_key(track, scene)
        if key is None or key in self._rec_pending:
            return  # кліп під запис ще не подія
        value = self._clip_prop_value(prop, self._safe_attr(clip, prop))
        if value is None:
            return
        current = self._mirror["clip_props"].setdefault(key, {})
        if current.get(prop) == value:
            return
        current[prop] = value
        payload = self._clip_refs(track, scene)
        payload["prop"] = prop
        payload["value"] = value
        # Ключ на пару (кліп, властивість): gain тягнуть мишею, і жест має
        # дати одну подію, але gain не має перекривати warp_mode.
        self._defer("clipprop:%s:%s" % (key, prop), "ClipPropSet", payload)

    def _prime_clip_props(self, track, scene, slot):
        key = self._clip_key(track, scene)
        if key is None:
            return
        try:
            if not slot.has_clip:
                self._mirror["clip_props"].pop(key, None)
                return
            self._mirror["clip_props"][key] = self._clip_props_state(slot.clip)
        except Exception:
            self._mirror["clip_props"].pop(key, None)

    def _prime_all_clip_props(self):
        self._mirror["clip_props"] = {}
        for track in self._doc.tracks:
            try:
                slots = list(track.clip_slots)
                scenes = list(self._doc.scenes)
            except Exception:
                continue
            for i, slot in enumerate(slots):
                if i >= len(scenes):
                    break
                self._prime_clip_props(track, scenes[i], slot)

    def _apply_clip_prop(self, payload, gseq):
        prop = payload.get("prop")
        if prop not in CLIP_PROPS:
            self._warn("gseq %s: невідома властивість кліпу %r" % (gseq, prop))
            return
        value = self._clip_prop_value(prop, payload.get("value"))
        if value is None:
            self._warn("gseq %s: некоректне значення %r для %s"
                       % (gseq, payload.get("value"), prop))
            return
        clip, key = self._resolve_any_clip(payload, gseq)
        if clip is None:
            return  # tombstone: кліпа немає, подія мовчки не діє
        if key is not None:
            self._mirror["clip_props"].setdefault(key, {})[prop] = value
        try:
            setattr(clip, prop, value)
        except Exception as e:
            # Половина властивостей існує лише в audio-кліпів: warp на MIDI
            # не помилка партнера, а різниця типів.
            self._warn("gseq %s: %s не встановився: %r" % (gseq, prop, e))
        if key is not None:
            actual = self._clip_prop_value(prop, self._safe_attr(clip, prop))
            if actual is not None:
                self._mirror["clip_props"][key][prop] = actual

    # ------------------------------------------------------------- локатори
    #
    # Адресуються ЧАСОМ, і це не спрощення. CuePoint.time доступний лише
    # на читання, set_data в нього немає, а два локатори не бувають на одній
    # позиції -- отже час і є ідентичність. Пересунути локатор не можна
    # взагалі: у Live це видалити й поставити заново.

    @staticmethod
    def _valid_cue_time(raw):
        """Одна перевірка на обидва напрямки.

        Розійшовшись, вони дали б найдурніший з можливих багів: ми емітимо
        локатор, який власний же приймальний бік відхиляє як некоректний.
        """
        try:
            value = round(float(raw), 6)
        except Exception:
            return None
        if not math.isfinite(value) or value < 0 or value > CLIP_LENGTH_MAX:
            return None
        return value

    def _cue_time(self, cue):
        return self._valid_cue_time(self._safe_attr(cue, "time"))

    def _cue_map(self):
        state = {}
        try:
            cues = list(self._doc.cue_points)
        except Exception:
            return state
        for cue in cues:
            time = self._cue_time(cue)
            if time is not None:
                state[time] = self._safe_name(cue)
        return state

    def _prime_cues(self):
        self._mirror["cues"] = self._cue_map()

    def _on_cues(self):
        if not self._registry_ready or self._suppress_struct:
            return
        previous = self._mirror.get("cues")
        current = self._cue_map()
        if previous is None:
            self._mirror["cues"] = current
            return
        self._mirror["cues"] = current
        for time, name in sorted(current.items()):
            if previous.get(time) != name:
                self._emit("CueSet", {"time": time, "name": name})
        for time in sorted(previous):
            if time not in current:
                self._emit("CueDelete", {"time": time})

    def _cue_at(self, time):
        try:
            for cue in self._doc.cue_points:
                if self._cue_time(cue) == time:
                    return cue
        except Exception:
            pass
        return None

    def _cue_payload_time(self, payload):
        return self._valid_cue_time(payload.get("time"))

    def _toggle_cue_at(self, time, gseq):
        """Перемикає локатор у заданій позиції. True -- зроблено або безнадійно.

        set_or_delete_cue працює у ПОТОЧНІЙ позиції, іншого шляху немає.
        Тобто щоб поставити локатор у партнера, доводиться зрушити його
        плейхед -- і зробити це доводиться в ОКРЕМОМУ тіку, бо запис
        current_song_time не застосовується миттєво.

        Під час відтворення ми туди не ліземо взагалі: подія чекає в черзі,
        доки транспорт зупиниться, бо смикати чужий плейхед на льоту чути.

        Позицію не відновлюємо: рух плейхеда при зупиненому транспорті --
        це те саме, що клацнути в лінійці, і повертати його назад означало б
        ще один такий самий стрибок.
        """
        landed = self._safe_attr(self._doc, "current_song_time")
        if landed is None or abs(float(landed) - float(time)) > CUE_POSITION_EPSILON:
            # Плейхед ще не там. Просимо Live його зрушити й ЙДЕМО ГЕТЬ:
            # запис current_song_time не встигає застосуватись у тому ж
            # тіку, а set_or_delete_cue працює в поточній позиції -- тобто
            # створив би локатор там, де плейхед лишився.
            #
            # Виміряно на живій парі: подія просила 52, плейхед лишився на
            # 128, і локатор зʼявився на 128 -- та ще й полетів назад подією.
            # Тому запит на рух і сам перемикач розведені по тіках.
            try:
                self._doc.current_song_time = time
            except Exception as e:
                self._warn("gseq %s: плейхед не рушив: %r" % (gseq, e))
                return True   # безнадійно, знімаємо з черги
            return False      # ще не зробили: спробуємо наступного тіка

        try:
            self._doc.set_or_delete_cue()
        except Exception as e:
            self._warn("gseq %s: локатор не перемкнувся: %r" % (gseq, e))
        return True

    def _apply_cue_set(self, payload, gseq):
        """True -- зроблено або безнадійно; False -- плейхед ще їде."""
        time = self._cue_payload_time(payload)
        if time is None:
            self._warn("gseq %s: некоректна позиція локатора %r" % (gseq, payload.get("time")))
            return True
        name = self._doc_str(payload.get("name") or "")
        if len(name) > 64:
            name = name[:64]
        cue = self._cue_at(time)
        self._mirror.setdefault("cues", {})[time] = name  # ДО запису -- глушимо ехо
        if cue is None:
            if not self._toggle_cue_at(time, gseq):
                return False   # плейхед ще їде, спробуємо наступного тіка
            cue = self._cue_at(time)
            if cue is None:
                self._warn("gseq %s: локатор на %s не створився" % (gseq, time))
                return True
        if name:
            try:
                cue.name = name
            except Exception as e:
                self._warn("gseq %s: локатор не перейменувався: %r" % (gseq, e))
        self._mirror["cues"] = self._cue_map()
        return True

    def _apply_cue_delete(self, payload, gseq):
        """True -- зроблено або безнадійно; False -- плейхед ще їде."""
        time = self._cue_payload_time(payload)
        if time is None:
            return True
        if self._cue_at(time) is None:
            return True  # tombstone: локатора вже немає
        self._mirror.setdefault("cues", {}).pop(time, None)
        if not self._toggle_cue_at(time, gseq):
            return False
        self._mirror["cues"] = self._cue_map()
        return True

    def _return_ids(self):
        """uuid Return-треків у порядку позицій."""
        out = []
        try:
            returns = list(self._doc.return_tracks)
        except Exception:
            return out
        for track in returns:
            out.append(self._aux_tracks_reg.id_of(track, create=False))
        return out

    def _diff_returns(self):
        """Поява й зникнення Return-треків -> ReturnCreate/ReturnDelete.

        Без цього набір Return-треків між машинами розходиться, а сенди
        адресуються ПОЗИЦІЄЮ -- отже той самий index означає інший ревер.
        Контрольна сума в MixerSet це ловить, але ловити краще те, чого
        не можна уникнути, а не те, що можна синхронізувати.
        """
        previous = self._mirror.get("returns")
        current = self._return_ids()
        if previous is None:
            self._mirror["returns"] = current
            return
        self._mirror["returns"] = current
        prev_set = set(x for x in previous if x)
        cur_set = set(x for x in current if x)
        for idx, rid in enumerate(current):
            if rid and rid not in prev_set:
                name = ""
                try:
                    name = self._safe_name(list(self._doc.return_tracks)[idx])
                except Exception:
                    pass
                self._emit("ReturnCreate", {"track": {"id": rid, "kind": "return"},
                                            "idx": idx, "name": name})
        for rid in previous:
            if rid and rid not in cur_set:
                self._emit("ReturnDelete", {"track": {"id": rid, "kind": "return"}})

    def _prime_returns(self):
        self._mirror["returns"] = self._return_ids()

    def _apply_return_create(self, payload, gseq):
        ref = payload.get("track") or {}
        uid = ref.get("id")
        if not uid:
            return
        if self._aux_tracks_reg.obj_of(uid) is not None:
            return  # ідемпотентність: такий Return уже є
        self._suppress_struct = True
        try:
            self._doc.create_return_track()
            returns = list(self._doc.return_tracks)
            if not returns:
                return
            created = returns[-1]   # LOM додає лише в кінець
            name = self._doc_str(payload.get("name") or "")
            if name:
                try:
                    created.name = name
                except Exception:
                    pass
            self._aux_tracks_reg.bind(uid, created)
        except Exception as e:
            self._warn("gseq %s: Return-трек не створився: %r" % (gseq, e))
        finally:
            self._suppress_struct = False
            self._after_returns_changed()

    def _apply_return_delete(self, payload, gseq):
        uid = (payload.get("track") or {}).get("id")
        target = self._aux_tracks_reg.obj_of(uid) if uid else None
        if target is None:
            return  # tombstone: такого Return уже немає
        try:
            idx = list(self._doc.return_tracks).index(target)
        except Exception:
            return
        self._suppress_struct = True
        try:
            self._doc.delete_return_track(idx)
        except Exception as e:
            self._warn("gseq %s: Return-трек не видалився: %r" % (gseq, e))
        finally:
            self._suppress_struct = False
            self._after_returns_changed()

    def _after_returns_changed(self):
        """Зміна набору Return-треків зсуває ВСІ сенди на всіх треках."""
        self._rewire_tracks()
        self._refresh_aux_tracks()
        self._prime_mixer()
        self._prime_returns()
        self._persist_registry()

    def _warp_markers(self, clip):
        """Маркери кліпу як список пар. None -- у кліпа їх немає (MIDI).

        beat_time -- доля, sample_time -- СЕКУНДА у файлі (виміряно: маркер
        на 64-й долі при 100 bpm дає 38.4). Пара портативна: вона описує
        відображення того самого файлу, а не позицію на диску.
        """
        try:
            markers = list(clip.warp_markers)
        except Exception:
            return None
        out = []
        for marker in markers[:WARP_MARKERS_MAX]:
            try:
                beat = round(float(marker.beat_time), 6)
                sample = round(float(marker.sample_time), 6)
            except Exception:
                continue
            if math.isfinite(beat) and math.isfinite(sample):
                out.append({"beat_time": beat, "sample_time": sample})
        out.sort(key=lambda m: m["beat_time"])
        return out

    def _make_warp_cb(self, track, scene, clip):
        def cb():
            self._safe(self._on_warp, track, scene, clip)
        return cb

    def _on_warp(self, track, scene, clip):
        if not self._registry_ready or self._suppress_struct:
            return
        key = self._clip_key(track, scene)
        if key is None or key in self._rec_pending:
            return
        markers = self._warp_markers(clip)
        if markers is None or self._mirror["warp"].get(key) == markers:
            return
        self._mirror["warp"][key] = markers
        payload = self._clip_refs(track, scene)
        payload["markers"] = markers
        # Маркер тягнуть мишею, і кожен рух чіпає сусідні -- один жест має
        # дати одну подію з повним набором.
        self._defer("warp:" + key, "ClipWarpSet", payload)

    def _prime_clip_warp(self, track, scene, slot):
        key = self._clip_key(track, scene)
        if key is None:
            return
        try:
            if not slot.has_clip:
                self._mirror["warp"].pop(key, None)
                return
            markers = self._warp_markers(slot.clip)
        except Exception:
            return
        if markers is None:
            self._mirror["warp"].pop(key, None)
        else:
            self._mirror["warp"][key] = markers

    def _prime_all_clip_warp(self):
        self._mirror["warp"] = {}
        for track in self._doc.tracks:
            try:
                slots = list(track.clip_slots)
                scenes = list(self._doc.scenes)
            except Exception:
                continue
            for i, slot in enumerate(slots):
                if i >= len(scenes):
                    break
                self._prime_clip_warp(track, scenes[i], slot)

    def _apply_clip_warp(self, payload, gseq):
        raw = payload.get("markers")
        if not isinstance(raw, list) or not raw or len(raw) > WARP_MARKERS_MAX:
            self._warn("gseq %s: некоректний набір warp-маркерів" % (gseq,))
            return
        want = []
        for entry in raw:
            try:
                beat = round(float((entry or {}).get("beat_time")), 6)
                sample = round(float((entry or {}).get("sample_time")), 6)
            except Exception:
                self._warn("gseq %s: маркер без beat_time/sample_time" % (gseq,))
                return
            if not math.isfinite(beat) or not math.isfinite(sample) or sample < 0:
                self._warn("gseq %s: маркер поза межами" % (gseq,))
                return
            want.append({"beat_time": beat, "sample_time": sample})
        want.sort(key=lambda m: m["beat_time"])

        clip, key = self._resolve_any_clip(payload, gseq)
        if clip is None:
            return  # tombstone
        try:
            if not clip.is_audio_clip:
                return  # warp-маркерів у MIDI немає: це різниця типів
        except Exception:
            return

        current = self._warp_markers(clip)
        if current is None or current == want:
            return
        make = self._warp_marker_factory(clip)
        if make is None:
            self._warn("gseq %s: маркери не застосовано -- у кліпі немає "
                       "жодного маркера, з якого взяти тип" % (gseq,))
            return   # дзеркала НЕ чіпаємо: розбіжність має лишитись видимою
        if key is not None:
            self._mirror["warp"][key] = want   # ДО запису -- глушимо ехо
        have = dict((m["beat_time"], m["sample_time"]) for m in current)
        # Спершу додаємо, потім прибираємо: кліп без жодного маркера Live
        # не приймає, і порожній проміжний стан коштував би нам маркерів.
        pending = self._add_warp_markers(clip, make, want, have)
        keep = set(m["beat_time"] for m in want)
        for beat in sorted(have):
            if beat in keep:
                continue
            try:
                clip.remove_warp_marker(beat)
            except Exception:
                pass  # Live не дає прибрати останній -- це не помилка
        # Друга спроба. Маркер часто не лізе не сам по собі, а через сусіда,
        # якого ми щойно прибрали: Live міряє відрізок між сусідами й на
        # нульовому каже "Segment length out of range". Так було з маркером
        # на 0.25 при живому маркері на 0 -- обидва з sample_time 0.
        if pending:
            pending = self._add_warp_markers(clip, make,
                                             [m for m, _ in pending], {})
        if pending:
            self._warn("gseq %s: не додано %d warp-маркер(ів), перший на "
                       "%s: %r" % (gseq, len(pending), pending[0][0]["beat_time"],
                                   pending[0][1]))
        if key is not None:
            actual = self._warp_markers(clip)
            if actual is not None:
                self._mirror["warp"][key] = actual

    @staticmethod
    def _add_warp_markers(clip, make, want, have):
        """Додає маркери, яких бракує. Віддає (маркер, помилка) для неприйнятих."""
        left = []
        for marker in want:
            if have.get(marker["beat_time"]) == marker["sample_time"]:
                continue
            try:
                clip.add_warp_marker(make(beat_time=marker["beat_time"],
                                          sample_time=marker["sample_time"]))
            except Exception as e:
                left.append((marker, e))
        return left

    @staticmethod
    def _warp_marker_factory(clip):
        """Конструктор TWarpMarker, узятий з наявного маркера кліпа.

        Свій тип із тими самими полями Live відкидає, а поля наявного
        маркера не мають сетера -- лишається тільки клас із живого обʼєкта.
        """
        try:
            markers = clip.warp_markers
            if not len(markers):
                return None
            return type(markers[0])
        except Exception:
            return None

    def _warn_missing_chain_device(self, gseq, track_ref, device_ref, chain_path):
        """Пояснює недосяжний девайс один раз на адресу, а не на кожну ручку.

        Один рух фейдера в раку -- це десятки DeviceParamSet. Коли рак у
        партнера інакший, кожна з них не знаходить цілі, і в лозі виростає
        стіна однакових рядків: на живому прогоні їх було 53 за сеанс, і з
        них неможливо було зрозуміти ані що зламалось, ані що робити.
        """
        addr = (track_ref.get("id") if track_ref else None,
                json.dumps(chain_path, sort_keys=True), str(device_ref))
        seen = self._missing_chain_warned.get(addr, 0)
        self._missing_chain_warned[addr] = seen + 1
        if seen:
            return   # про цю саму адресу вже сказано

        name = (device_ref or {}).get("class_display_name") or "девайс"
        if chain_path:
            self._warn(
                "gseq %s: %s усередині раку недосяжний -- ланцюга за адресою "
                "%r тут немає. Найчастіша причина: рак у партнера має інший "
                "вміст (інший кит, інший набір падів). Значення всередині "
                "поїдуть, щойно рак стане однаковим; структуру раку синк не "
                "переносить -- завантаж той самий."
                % (gseq, name, chain_path))
        else:
            self._warn("gseq %s: %s на треку недосяжний, подію пропущено"
                       % (gseq, name))

    def _wire_arrangement_clips(self, track):
        """Підписки на кліпи в лінійці. Адреса в них -- uuid, не сцена."""
        track_ref = self._device_track_ref(track)
        if not track_ref:
            return
        for clip in self._arr_clips(track):
            uid = self._arr_reg.id_of(clip, create=False)
            if not uid:
                continue
            for prop in CLIP_PROPS:
                self._listen(clip, prop, self._make_arr_prop_cb(track_ref, clip, uid, prop))
            self._listen(clip, "warp_markers", self._make_arr_warp_cb(track_ref, clip, uid))
            loop_cb = self._make_arr_loop_cb(track_ref, clip, uid)
            for prop in CLIP_LOOP_PROPS:
                self._listen(clip, prop, loop_cb)
            try:
                if clip.is_midi_clip:
                    self._listen(clip, "notes", self._make_arr_notes_cb(track_ref, clip, uid))
            except Exception:
                pass
            self._wire_metadata("clip", clip)

    def _make_arr_prop_cb(self, track_ref, clip, uid, prop):
        def cb():
            self._safe(self._on_arr_prop, track_ref, clip, uid, prop)
        return cb

    def _make_arr_notes_cb(self, track_ref, clip, uid):
        def cb():
            self._safe(self._on_arr_notes, track_ref, clip, uid)
        return cb

    def _on_arr_notes(self, track_ref, clip, uid):
        """Редагування нот у кліпі лінійки.

        Досі ноти звідти їхали лише один раз -- разом зі створенням кліпа.
        Тобто перетягнути кліп в Arrangement партнер бачив, а домалювати
        в ньому ноту -- ні.
        """
        if not self._registry_ready or self._suppress_struct:
            return
        now = time.time()
        key = "arr:" + uid
        previous = self._note_pending.get(key)
        self._note_pending[key] = {
            "arr": True,
            "track_ref": track_ref,
            "clip": clip,
            "uid": uid,
            "due": now + DEBOUNCE_SEC,
            "first": previous["first"] if previous else now,
        }

    def _flush_arr_notes(self, key, pending):
        clip = pending["clip"]
        uid = pending["uid"]
        try:
            current = self._clip_notes(clip)
        except Exception:
            return
        previous = self._mirror["notes"].get(key)
        self._mirror["notes"][key] = current
        if previous is None:
            return  # щойно побачений кліп -- це базова лінія, а не правка
        for region in sorted(self._changed_note_regions(previous, current)):
            from_pitch, pitch_span, from_time, time_span = region
            self._emit("ArrangementClipNotesSet", {
                "track": pending["track_ref"],
                "clip": {"id": uid},
                "region": {"from_pitch": from_pitch, "pitch_span": pitch_span,
                           "from_time": from_time, "time_span": time_span},
                "notes": self._notes_in_region(current, region),
            })

    def _make_arr_loop_cb(self, track_ref, clip, uid):
        def cb():
            self._safe(self._on_arr_loop, track_ref, clip, uid)
        return cb

    def _on_arr_loop(self, track_ref, clip, uid):
        if not self._registry_ready or self._suppress_struct:
            return
        state = self._clip_loop_state(clip)
        key = "arr:" + uid
        if state is None or self._mirror["loop"].get(key) == state:
            return
        self._mirror["loop"][key] = state
        payload = {"track": track_ref, "clip": {"id": uid}}
        payload.update(state)
        self._defer("loop:" + key, "ClipLoopSet", payload)

    def _make_arr_warp_cb(self, track_ref, clip, uid):
        def cb():
            self._safe(self._on_arr_warp, track_ref, clip, uid)
        return cb

    def _on_arr_prop(self, track_ref, clip, uid, prop):
        if not self._registry_ready or self._suppress_struct:
            return
        value = self._clip_prop_value(prop, self._safe_attr(clip, prop))
        if value is None:
            return
        key = "arr:" + uid
        current = self._mirror["clip_props"].setdefault(key, {})
        if current.get(prop) == value:
            return
        current[prop] = value
        self._defer("clipprop:%s:%s" % (key, prop), "ClipPropSet", {
            "track": track_ref, "clip": {"id": uid}, "prop": prop, "value": value,
        })

    def _on_arr_warp(self, track_ref, clip, uid):
        if not self._registry_ready or self._suppress_struct:
            return
        markers = self._warp_markers(clip)
        key = "arr:" + uid
        if markers is None or self._mirror["warp"].get(key) == markers:
            return
        self._mirror["warp"][key] = markers
        self._defer("warp:" + key, "ClipWarpSet", {
            "track": track_ref, "clip": {"id": uid}, "markers": markers,
        })

    def _prime_arrangement_clips(self):
        """Базова лінія для кліпів у лінійці -- разом із сесійними."""
        for track in self._doc.tracks:
            track_ref = self._device_track_ref(track)
            if not track_ref:
                continue
            for clip in self._arr_clips(track):
                uid = self._arr_reg.id_of(clip, create=False)
                if not uid:
                    continue
                key = "arr:" + uid
                self._mirror["clip_props"][key] = self._clip_props_state(clip)
                loop = self._clip_loop_state(clip)
                if loop is not None:
                    self._mirror["loop"][key] = loop
                try:
                    if clip.is_midi_clip:
                        self._mirror["notes"][key] = self._clip_notes(clip)
                except Exception:
                    pass
                markers = self._warp_markers(clip)
                if markers is not None:
                    self._mirror["warp"][key] = markers

    def _chain_mix_param(self, chain, param):
        """DeviceParameter мікшера ланцюга, або None."""
        md = self._safe_attr(chain, "mixer_device")
        if md is None:
            return None
        return self._safe_attr(md, param)

    def _chain_state(self, chain):
        """Гучність, панорама й перемикачі ланцюга -- усе, що чути."""
        state = {}
        for param in CHAIN_MIX_PARAMS:
            p = self._chain_mix_param(chain, param)
            if p is None:
                continue
            try:
                state[param] = round(float(p.value), 6)
            except Exception:
                continue
        for prop in CHAIN_TOGGLES:
            value = self._safe_attr(chain, prop)
            if value is not None:
                state[prop] = bool(value)
        # Назва й колір -- метадані, вони їдуть ObjectMetaSet, але в знімку
        # мають бути поруч: інакше pull лишав би пади без підписів.
        name = self._safe_name(chain)
        if name:
            state["name"] = name
        color = self._safe_color(chain)
        if color is not None:
            state["color"] = color
        return state

    def _make_chain_cb(self, chain, uid, key):
        def cb():
            self._safe(self._on_chain, chain, uid, key)
        return cb

    def _on_chain(self, chain, uid, key):
        if not self._registry_ready or self._suppress_struct:
            return
        if key in CHAIN_TOGGLES:
            value = self._safe_attr(chain, key)
            if value is None:
                return
            value = bool(value)
        else:
            p = self._chain_mix_param(chain, key)
            if p is None:
                return
            try:
                value = round(float(p.value), 6)
            except Exception:
                return
        mirror = self._mirror["chain"].setdefault(uid, {})
        if mirror.get(key) == value:
            return
        mirror[key] = value
        payload = {"chain": {"id": uid}, "param": key, "value": value}
        if key in CHAIN_TOGGLES:
            # дискретний перемикач -- дебаунс лише додав би затримки
            self._emit("ChainMixerSet", payload)
        else:
            self._defer("chain:%s:%s" % (uid, key), "ChainMixerSet", payload)

    def _wire_chain_mixer(self, chain, uid):
        for param in CHAIN_MIX_PARAMS:
            p = self._chain_mix_param(chain, param)
            if p is not None:
                self._listen(p, "value", self._make_chain_cb(chain, uid, param))
        for prop in CHAIN_TOGGLES:
            self._listen(chain, prop, self._make_chain_cb(chain, uid, prop))

    def _prime_chains_mix(self):
        state = {}
        for rec in self._chain_records:
            uid = rec.get("id")
            chain = self._chains_reg.obj_of(uid) if uid else None
            if chain is None:
                continue
            state[uid] = self._chain_state(chain)
        self._mirror["chain"] = state

    def _apply_chain_mixer(self, payload, gseq):
        uid = (payload.get("chain") or {}).get("id")
        chain = self._chains_reg.obj_of(uid) if uid else None
        if chain is None:
            return  # tombstone: ланцюга немає, подія мовчки не діє
        param = payload.get("param")
        if param in CHAIN_TOGGLES:
            value = payload.get("value")
            if not isinstance(value, bool):
                self._warn("gseq %s: %s має бути булевим" % (gseq, param))
                return
            self._mirror["chain"].setdefault(uid, {})[param] = value
            try:
                setattr(chain, param, value)
            except Exception as e:
                self._warn("gseq %s: %s ланцюга не встановився: %r" % (gseq, param, e))
            return
        if param not in CHAIN_MIX_PARAMS:
            self._warn("gseq %s: невідомий параметр ланцюга %r" % (gseq, param))
            return
        p = self._chain_mix_param(chain, param)
        if p is None:
            return
        try:
            value = float(payload.get("value"))
        except Exception:
            return
        # Межі беремо з самого параметра: у гучності й панорами вони різні
        value = max(float(p.min), min(float(p.max), value))
        self._mirror["chain"].setdefault(uid, {})[param] = round(value, 6)
        try:
            p.value = value
        except Exception as e:
            self._warn("gseq %s: %s ланцюга не встановився: %r" % (gseq, param, e))

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
            self._request_rewire()
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
                self._safe(self._emit_sample_load_slot, clip, refs)

        # has_clip changed: the old clip listener is dead or a new one is needed.
        self._request_rewire()
        self._prime_metadata()
        self._prime_clip_loops()
        self._prime_stop_buttons()
        self._prime_all_clip_props()
        self._prime_all_clip_warp()
        self._prime_arrangement_clips()
        self._prime_chains_mix()
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
            if pending.get("arr"):
                # Кліп у лінійці адресується uuid, і регіони емітяться окремим типом
                self._safe(self._flush_arr_notes, key, pending)
                continue
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

    def _make_stop_button_cb(self, track, scene, slot):
        def cb():
            self._safe(self._on_stop_button, track, scene, slot)
        return cb

    def _stop_button_state(self, slot):
        value = self._safe_attr(slot, "has_stop_button")
        return None if value is None else bool(value)

    def _on_stop_button(self, track, scene, slot):
        """Стоп-кнопка слота змінилась.

        Дискретний перемикач, тож без дебаунсу: один клац -- одна подія.
        """
        if not self._registry_ready or self._suppress_struct:
            return
        key = self._clip_key(track, scene)
        if key is None:
            return
        value = self._stop_button_state(slot)
        if value is None or self._mirror["stopbtn"].get(key) == value:
            return
        self._mirror["stopbtn"][key] = value
        payload = self._clip_refs(track, scene)
        payload["value"] = value
        self._emit("SlotStopButtonSet", payload)

    def _prime_stop_button(self, track, scene, slot):
        key = self._clip_key(track, scene)
        if key is None:
            return
        value = self._stop_button_state(slot)
        if value is None:
            self._mirror["stopbtn"].pop(key, None)
        else:
            self._mirror["stopbtn"][key] = value

    def _prime_stop_buttons(self):
        """Без прайму перший же перегляд слота виглядав би як зміна проти None."""
        self._mirror["stopbtn"] = {}
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
                self._prime_stop_button(track, scene, slots[i])

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
            self._prime_clip_props(track, scene, slot)
            self._prime_clip_warp(track, scene, slot)
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
            self._prime_clip_props(track, scene, slot)
            self._prime_clip_warp(track, scene, slot)
            raise

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

        elif etype == "ChainMixerSet":
            self._apply_chain_mixer(payload, gseq)

        elif etype == "ClipWarpSet":
            self._apply_clip_warp(payload, gseq)

        elif etype == "ReturnCreate":
            self._apply_return_create(payload, gseq)

        elif etype == "ReturnDelete":
            self._apply_return_delete(payload, gseq)

        elif etype == "CueSet":
            self._queue_cue("CueSet", payload, gseq)

        elif etype == "CueDelete":
            self._queue_cue("CueDelete", payload, gseq)

        elif etype == "ClipPropSet":
            self._apply_clip_prop(payload, gseq)

        elif etype == "SceneTimingSet":
            self._apply_scene_timing(payload, gseq)

        elif etype == "SongPropSet":
            self._apply_song_prop(payload, gseq)

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

        elif etype == "TrackDuplicate":
            ref = payload.get("track") or {}
            uid = ref.get("id")
            if not uid or self._tracks_reg.obj_of(uid) is not None:
                return  # ідемпотентність: копія вже є
            source, _sref = self._resolve_device_track(payload.get("source") or {})
            self._suppress_struct = True
            try:
                if source is None:
                    # Джерела немає -- але дія користувача була. Порожній трек
                    # ламає лише DeviceParamSet, а відсутній -- узагалі все,
                    # що на нього адресується.
                    self._warn("gseq %s: джерело для дубля невідоме, роблю порожній трек"
                               % (gseq,))
                    idx = len(self._doc.tracks)
                    if payload.get("kind") == "midi":
                        self._doc.create_midi_track(idx)
                    elif payload.get("kind") == "audio":
                        self._doc.create_audio_track(idx)
                    else:
                        return
                    new = self._doc.tracks[idx]
                else:
                    idx = self._track_index(source)
                    if idx is None:
                        return
                    self._doc.duplicate_track(idx)
                    new = self._doc.tracks[idx + 1]
                if isinstance(ref.get("name"), str):
                    new.name = ref["name"]
                color = ref.get("color")
                if isinstance(color, int) and not isinstance(color, bool) and 0 <= color <= 0xFFFFFF:
                    new.color = color
                self._tracks_reg.bind(uid, new)
                # Одразу перезаписуємо успадкований від джерела id, інакше два
                # обʼєкти претендуватимуть на нього до наступного персисту.
                self._obj_store_id(new, uid)
            finally:
                self._suppress_struct = False
                self._diff_tracks(emit=False)
                self._refresh_aux_tracks()
                self._refresh_chains()
                self._persist_registry()
                self._prime_mixer()
                self._prime_devices()
                self._prime_samples()
                self._prime_device_state()
                self._prime_notes()
                self._prime_metadata()
                self._prime_clip_loops()
                self._prime_stop_buttons()
                self._prime_all_clip_props()
                self._prime_all_clip_warp()
                self._prime_arrangement_clips()
                self._prime_chains_mix()

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
            elif kind == "chain":
                uid = (payload.get("chain") or {}).get("id")
                target = self._chains_reg.obj_of(uid) if uid else None
            elif kind == "clip":
                # Кліп у лінійці адресується власним uuid: сцен там немає.
                if (payload.get("clip") or {}).get("id"):
                    track, target = self._resolve_arr_clip(payload)
                else:
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
                self._prime_clip_props(track, scene, slot)
                self._prime_clip_warp(track, scene, slot)
                self._prime_metadata()
                self._prime_clip_loops()
                self._prime_stop_buttons()
                self._prime_all_clip_props()
                self._prime_all_clip_warp()
                self._prime_arrangement_clips()
                self._prime_chains_mix()

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
                self._prime_clip_props(track, scene, slot)
                self._prime_clip_warp(track, scene, slot)
                self._prime_metadata()
                self._prime_clip_loops()
                self._prime_stop_buttons()
                self._prime_all_clip_props()
                self._prime_all_clip_warp()
                self._prime_arrangement_clips()
                self._prime_chains_mix()

        elif etype == "SampleLoad":
            self._queue_device_struct(etype, payload, gseq)

        elif etype == "DeviceLoad":
            # У чергу, а не одразу: завантаження блокує Live на сотні мілісекунд
            # і рухає виділення, а під запис виділений трек чіпати не можна.
            self._queue_device_load(payload, gseq)

        elif etype in ("DeviceInsert", "DeviceMove"):
            # insert_device виділення не рухає, тож guard на запис тут не потрібен.
            # Черга лишається: завантаження важкого інструмента однаково блокує
            # Live, і залп із журналу підвісив би його так само, як залп load_item.
            self._queue_device_struct(etype, payload, gseq)

        elif etype == "DeviceDelete":
            # Видалення дешеве й миттєве -- у чергу його ставити нема за чим.
            self._safe(self._apply_device_delete, payload, gseq)

        elif etype == "ArrangementClipCreate":
            self._apply_arr_create(payload, gseq)

        elif etype == "ArrangementClipMove":
            self._apply_arr_move(payload, gseq)

        elif etype == "ArrangementClipDelete":
            self._apply_arr_delete(payload, gseq)

        elif etype == "ArrangementClipNotesSet":
            self._apply_arr_notes(payload, gseq)

        elif etype == "ClipLoopSet":
            # Кліп у лінійці адресується uuid, сесійний -- сценою.
            clip, _key = self._resolve_any_clip(payload, gseq)
            if clip is None:
                self._warn("gseq %s: кліпу немає, межі нема на що класти" % (gseq,))
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

            if _key is not None:
                self._mirror["loop"][_key] = dict(state)
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
            if _key is not None:
                actual = self._clip_loop_state(clip)
                if actual is not None:
                    self._mirror["loop"][_key] = actual

        elif etype == "DeviceStateSet":
            prop = payload.get("prop")
            if prop not in DEVICE_STATE_PROPS:
                return self._warn("gseq %s: невідома властивість девайса %r" % (gseq, prop))
            track, _tref = self._resolve_device_track(payload.get("track"))
            device = self._resolve_device_only(track, payload.get("chain_path"),
                                               payload.get("device"))
            if device is None:
                return  # tombstone: девайса немає, подія мовчки не діє
            if not hasattr(device, prop):
                return self._warn("gseq %s: у девайса немає %s" % (gseq, prop))
            value = payload.get("value")
            if not isinstance(value, bool) and not isinstance(value, (int, float)):
                return self._warn("gseq %s: %s має бути числом або булевим" % (gseq, prop))
            if not isinstance(value, bool):
                value = float(value)
                if not math.isfinite(value):
                    return self._warn("gseq %s: %s не є скінченним" % (gseq, prop))
                value = int(value) if value.is_integer() else round(value, 6)

            track_ref = self._device_track_ref(track)
            _c, device_ref, chain_path = self._device_location(track, device)
            key = self._sample_key(track_ref, chain_path, device_ref, prop)
            self._mirror["devstate"][key] = value   # ДО запису -- глушимо ехо
            try:
                setattr(device, prop, value)
            except Exception as e:
                self._warn("gseq %s: %s не записався: %r" % (gseq, prop, e))
            # Live мовчки клампить значення поза діапазоном -- дзеркало має
            # відповідати тому, що вийшло, інакше наступна зміна не помітиться.
            actual = self._device_state_value(device, prop)
            if actual is None:
                self._mirror["devstate"].pop(key, None)
            else:
                self._mirror["devstate"][key] = actual

        elif etype == "SamplePropSet":
            prop = payload.get("prop")
            if prop not in SAMPLE_PROPS:
                return self._warn("gseq %s: невідома властивість семплу %r" % (gseq, prop))
            track, _tref = self._resolve_device_track(payload.get("track"))
            device = self._resolve_device_only(track, payload.get("chain_path"),
                                               payload.get("device"))
            if device is None:
                return  # tombstone: девайса немає, подія мовчки не діє
            sample = self._sample_of(device)
            if sample is None:
                return self._warn("gseq %s: у девайса немає семплу" % (gseq,))
            value = payload.get("value")
            if prop in SAMPLE_BOOL_PROPS:
                if not isinstance(value, bool):
                    return self._warn("gseq %s: %s має бути булевим" % (gseq, prop))
            else:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    return self._warn("gseq %s: %s має бути числом" % (gseq, prop))
                value = float(value)
                if not math.isfinite(value) or value < 0:
                    return self._warn("gseq %s: %s поза межами" % (gseq, prop))
                # Маркери задані в семплах, тож стеля -- довжина самого файлу.
                # Чужий кліп може бути довшим; тоді запис просто не має сенсу.
                if prop in ("start_marker", "end_marker"):
                    length = self._sample_prop_value(sample, "length")
                    if length is None:
                        try:
                            length = int(sample.length)
                        except Exception:
                            length = None
                    if length is not None and value > length:
                        return self._warn(
                            "gseq %s: %s=%s за межами семплу (%s)"
                            % (gseq, prop, value, length))
                value = int(value) if prop in SAMPLE_INT_PROPS else round(value, 6)

            track_ref = self._device_track_ref(track)
            _c, device_ref, chain_path = self._device_location(track, device)
            key = self._sample_key(track_ref, chain_path, device_ref, prop)
            self._mirror["sample"][key] = value   # ДО запису -- глушимо ехо
            try:
                setattr(sample, prop, value)
            except Exception as e:
                self._warn("gseq %s: %s не записався: %r" % (gseq, prop, e))
                actual = self._sample_prop_value(sample, prop)
                if actual is None:
                    self._mirror["sample"].pop(key, None)
                else:
                    self._mirror["sample"][key] = actual

        elif etype == "SlotStopButtonSet":
            value = payload.get("value")
            if not isinstance(value, bool):
                return self._warn("gseq %s: стоп-кнопка має бути булевою" % (gseq,))
            track, scene, slot = self._resolve_clip_slot(payload, gseq)
            if slot is None:
                return
            key = self._clip_key(track, scene)
            if key is not None:
                self._mirror["stopbtn"][key] = value   # ДО запису -- глушимо ехо
            try:
                slot.has_stop_button = value
            except Exception as e:
                self._warn("gseq %s: стоп-кнопка не записалась: %r" % (gseq, e))
                if key is not None:
                    actual = self._stop_button_state(slot)
                    if actual is None:
                        self._mirror["stopbtn"].pop(key, None)
                    else:
                        self._mirror["stopbtn"][key] = actual

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
            if param == "crossfade_assign":
                try:
                    want = int(payload.get("value"))
                except Exception:
                    return
                if want not in (0, 1, 2):
                    self._warn("gseq %s: crossfade_assign %r поза межами" % (gseq, want))
                    return
                md = self._safe_attr(track, "mixer_device")
                if md is None:
                    return
                self._mirror["mix"][self._mix_key(track_ref, param, None)] = want
                try:
                    md.crossfade_assign = want
                except Exception as e:
                    self._warn("gseq %s: crossfade_assign не встановився: %r" % (gseq, e))
                return
            if param == "send":
                want = (payload.get("return") or {}).get("id")
                mine = (self._send_return_ref(idx) or {}).get("id")
                if want and mine and want != mine:
                    self._warn("gseq %s: сенд %s веде в РІЗНІ Return-треки: "
                               "у партнера %s, у тебе %s -- набір Return-треків "
                               "розійшовся, вирівняй його вручну"
                               % (gseq, idx, want, mine))
                    return
                if want and not mine:
                    self._warn("gseq %s: сенда %s у тебе немає -- у партнера "
                               "більше Return-треків" % (gseq, idx))
                    return
            p = self._mix_param(track, param, idx)
            if p is None:
                self._warn("gseq %s: параметр %r/%r відсутній" % (gseq, param, idx))
                return
            try:
                value = float(payload.get("value"))
            except Exception:
                return
            # DeviceParameter кидає при виході за межі, а межі send-ів
            # відрізняються від volume -- беремо їх з самого параметра
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
                self._warn_missing_chain_device(gseq, track_ref, device_ref, chain_path)
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
                self._warn("gseq %s: невідомий перемикач %r" % (gseq, prop))
                return
            value = bool(payload.get("value"))
            self._mirror["mix"][self._toggle_key(track_ref, prop)] = value
            try:
                setattr(track, prop, value)
            except Exception as e:
                self._warn("gseq %s: %s не встановлюється: %r" % (gseq, prop, e))

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
        if self._chat is not None:
            self._chat.poll(self._handle_chat_request)
        if self._link is None or not self._link.alive:
            return
        for msg in self._link.poll():
            self._safe(self._dispatch, msg)
        if self._rewire_pending:
            self._safe(self._rewire_tracks)
        self._safe(self._flush_clips)
        self._safe(self._flush_notes)
        self._safe(self._flush_recording_clips)
        self._safe(self._flush_device_loads)
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
            etype = msg.get("type")
            payload = msg.get("payload") or {}
            # Прогалину рахуємо ДО застосування: _apply на нерозвʼязану адресу
            # мовчки виходить -- це правильна tombstone-семантика, але автор
            # події через неї не дізнається нічого. Він крутить ручку, у
            # партнера нічого не рухається, і жоден із двох не бачить причини.
            gap = self._safe(self._op_gap, etype, payload)
            try:
                self._apply(etype, payload, gseq)
            except Exception as e:
                self._link.send({"m": "apply_ack", "gseq": gseq,
                                 "ok": False, "error": repr(e)})
                raise
            ack = {"m": "apply_ack", "gseq": gseq, "ok": True}
            if gap:
                ack["gap"] = gap
                ack["type"] = etype
            self._link.send(ack)
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
            self._link.send(self._hello_payload())
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

    # ---------------------------------------------------------------- семпли
    #
    # Портативна адреса семпла -- шлях відносно теки проєкту. Виміряно на
    # живому 12.3: вміст проєкту адресується в браузері як
    # query:CurrentProject#Samples:Imported:file.wav, тобто шляхом, а не
    # машинним FileId (той -- доля Core Library). Байти возить filesync,
    # структуру -- ця подія.

    def _project_root(self):
        try:
            path = str(self._doc.file_path)
        except Exception:
            return ""
        return os.path.dirname(path) if path else ""

    def _sample_rel_path(self, path):
        """Шлях семпла відносно проєкту, або None, якщо він зовні.

        Зовнішній семпл не має портативної адреси взагалі: абсолютний шлях
        у партнера не існує. Тому ми його не анонсуємо, а показуємо в звіті
        _scan_samples із порадою Collect All and Save.
        """
        root = self._project_root()
        if not root or not path:
            return None
        try:
            root_abs = os.path.abspath(root)
            path_abs = os.path.abspath(str(path))
        except Exception:
            return None
        prefix = os.path.normcase(root_abs)
        if not prefix.endswith(os.sep):
            prefix += os.sep
        if not os.path.normcase(path_abs).startswith(prefix):
            return None
        try:
            rel = os.path.relpath(path_abs, root_abs)
        except Exception:
            return None
        return rel.replace(os.sep, "/")

    def _project_browser_item(self, rel_path):
        """BrowserItem за шляхом відносно проєкту, або None.

        Обхід за назвами: назви вузлів у браузері збігаються з іменами файлів
        разом із розширенням (перевірено). Збирати uri самотужки не варто --
        він потребує url-кодування, а помилка в ньому мовчазна.
        """
        parts = [p for p in str(rel_path or "").split("/") if p]
        if not parts:
            return None
        try:
            node = Live.Application.get_application().browser.current_project
        except Exception:
            return None
        for part in parts:
            try:
                children = list(node.children)
            except Exception:
                return None
            found = None
            for child in children:
                try:
                    if str(child.name) == part:
                        found = child
                        break
                except Exception:
                    continue
            if found is None:
                return None
            node = found
        try:
            return node if node.is_loadable else None
        except Exception:
            return None

    def _apply_sample_load(self, payload, gseq):
        """Кладе семпл у виділену ціль. Механізм перевірений на живому 12.3.

        Стадія 1 -- слот Session. Прицілювання через highlighted_clip_slot,
        далі browser.load_item, і Live сам створює audio-кліп. Той самий
        шлях, що робить рука.
        """
        sample = payload.get("sample") or {}
        rel = sample.get("path")
        target = payload.get("target") or {}

        # Слот -- найпростіший випадок, і для нього браузер не потрібен
        # узагалі: ClipSlot.create_audio_clip бере абсолютний шлях напряму.
        # Перевірено на 12.3.5 і 12.4.3.
        #
        # Це не оптимізація. На macOS вузол браузера "Current Project" буває
        # ПОРОЖНІМ навіть при відкритому проєкті -- виміряно на живій парі,
        # і через це семпли з Windows не доїжджали до Mac узагалі: подія
        # мовчки крутилась у черзі три хвилини й здавалась. Прямий шлях
        # цього не залежить, а заразом не смикає виділення.
        if target.get("kind") == "slot":
            return self._apply_sample_to_slot(payload, rel, gseq)

        item = self._project_browser_item(rel)
        if item is None:
            # Файл ще не доїхав або лежить не там: черга спробує ще раз
            return False
        if target.get("kind") == "drum_pad":
            self._apply_sample_to_pad(payload, target, item, gseq)
            return True
        if target.get("kind") == "arrangement":
            self._apply_sample_to_arrangement(payload, target, item, gseq)
            return True
        self._warn("gseq %s: невідома ціль для семпла %r"
                   % (gseq, target.get("kind")))
        return True

    def _project_file_path(self, rel):
        """Абсолютний шлях до семпла в теці проєкту, або None.

        Розділювач беремо з os.path: подія несе шлях через скісну риску
        завжди (так домовлено в протоколі), а от локальна файлова система
        може вимагати іншого.
        """
        parts = [p for p in str(rel or "").split("/") if p]
        if not parts:
            return None
        try:
            als = self._doc_str(self._doc.file_path)
        except Exception:
            return None
        if not als:
            return None
        folder = os.path.dirname(als)
        full = os.path.join(folder, *parts)
        return full if os.path.exists(full) else None

    def _apply_sample_to_slot(self, payload, rel, gseq):
        """Audio-кліп у слот напряму, без браузера. False -- файл ще не тут."""
        full = self._project_file_path(rel)
        if full is None:
            return False   # файл ще їде filesync-ом, черга спробує ще раз
        track, scene, slot = self._resolve_clip_slot(payload, gseq)
        if slot is None:
            return True
        try:
            if slot.has_clip:
                return True  # ідемпотентність: у слоті вже щось є
        except Exception:
            return True
        struct_was = self._suppress_struct
        self._suppress_struct = True
        try:
            slot.create_audio_clip(full)
        except Exception as e:
            self._warn("gseq %s: audio-кліп не створився: %r" % (gseq, e))
        finally:
            self._suppress_struct = struct_was
            self._rewire_tracks()
            self._prime_metadata()
            self._prime_clip_loops()
            self._prime_stop_buttons()
            self._prime_all_clip_props()
            self._prime_all_clip_warp()
            self._prime_arrangement_clips()
            self._prime_chains_mix()
        return True

    def _apply_sample_to_arrangement(self, payload, target, item, gseq):
        """Семпл на лінійку. Той самий тимчасовий слот, що й для MIDI.

        Створити кліп в Arrangement з нічого LOM не вміє: потрібне джерело.
        Для MIDI ми його ліпимо порожнім, тут -- вантажимо в нього семпл,
        а далі той самий duplicate_clip_to_arrangement. Шлях перевірений
        на живому 12.3.5 покроково.
        """
        uid = (payload.get("clip") or {}).get("id")
        start = self._arr_time_from_payload(target)
        if not uid or start is None:
            return
        track, _tref = self._resolve_device_track(payload.get("track") or {})
        if track is None:
            return
        if self._arr_reg.obj_of(uid) is not None:
            return  # ідемпотентність: кліп уже на місці
        slot = self._arr_free_slot(track)
        if slot is None:
            self._warn("gseq %s: усі слоти Session зайняті, немає де зібрати "
                       "джерело для Arrangement" % (gseq,))
            return

        view = getattr(self._doc, "view", None)
        if view is None:
            return
        saved_slot = self._safe_attr(view, "highlighted_clip_slot")
        saved_track = self._safe_attr(view, "selected_track")
        struct_was = self._suppress_struct
        self._suppress_struct = True
        self._suppress_view = True
        self._view_applied_at = time.time()
        try:
            view.selected_track = track
            view.highlighted_clip_slot = slot
            Live.Application.get_application().browser.load_item(item)
            source = slot.clip if slot.has_clip else None
            if source is None:
                self._warn("gseq %s: семпл не створив кліпу-джерела" % (gseq,))
            else:
                placed = self._arr_place(track, source, start, gseq)
                if placed is not None:
                    self._arr_reg.bind(uid, placed)
            try:
                slot.delete_clip()
            except Exception as e:
                self._warn("gseq %s: тимчасовий кліп не прибрався: %r" % (gseq, e))
        except Exception as e:
            self._warn("gseq %s: семпл не ліг у лінійку: %r" % (gseq, e))
        finally:
            try:
                if saved_slot is not None:
                    view.highlighted_clip_slot = saved_slot
                elif saved_track is not None:
                    view.selected_track = saved_track
            except Exception:
                pass
            self._view_applied_at = time.time()
            self._suppress_view = False
            self._mirror["view"] = self._view_signature()
            self._arr_after_write()
            self._suppress_struct = struct_was

    def _emit_sample_load_slot(self, clip, refs):
        """Audio-кліп у слоті -> SampleLoad, якщо семпл усередині проєкту."""
        path = self._safe_attr(clip, "file_path")
        rel = self._sample_rel_path(str(path) if path else "")
        if rel is None:
            self._warn("audio-кліп із семплом поза текою проєкту не синхронізується; "
                       "полагодь через File > Collect All and Save")
            return
        payload = dict(refs)
        payload["target"] = {"kind": "slot"}
        payload["sample"] = {"path": rel, "name": rel.rsplit("/", 1)[-1]}
        self._emit("SampleLoad", payload)

    def _pad_sample_path(self, pad):
        """Шлях семпла на паді, або None. Порожній пад -- теж None."""
        try:
            chains = list(pad.chains)
        except Exception:
            return None
        for chain in chains:
            try:
                devices = list(chain.devices)
            except Exception:
                continue
            for device in devices:
                sample = self._safe_attr(device, "sample")
                if sample is None:
                    continue
                path = self._safe_attr(sample, "file_path")
                if path:
                    return str(path)
        return None

    def _iter_drum_racks(self):
        """(track, track_ref, container, device, chain_path) для кожного Drum Rack."""
        for track in self._doc.tracks:
            track_ref = self._device_track_ref(track)
            if not track_ref:
                continue
            for container, device, chain_path in self._iter_track_devices(track):
                try:
                    if not device.can_have_drum_pads or not device.has_drum_pads:
                        continue
                except Exception:
                    continue
                yield track, track_ref, container, device, chain_path

    def _drum_pad_map(self):
        """Знімок вмісту падів: адреса рака -> {нота: шлях семплу}.

        Ключ адреси -- рівно те, чим подія адресує рак у партнера, тож
        розбіжність між знімком і подією неможлива за побудовою.
        """
        state = {}
        for _track, track_ref, container, device, chain_path in self._iter_drum_racks():
            device_ref = self._device_ref(container, device)
            if device_ref is None:
                continue
            key = (track_ref.get("id"), track_ref.get("kind"),
                   tuple(c.get("id") for c in chain_path),
                   device_ref["class_name"], device_ref["class_display_name"],
                   device_ref["ordinal"])
            pads = {}
            try:
                drum_pads = list(device.drum_pads)
            except Exception:
                drum_pads = []
            for pad in drum_pads:
                path = self._pad_sample_path(pad)
                if not path:
                    continue
                note = self._safe_attr(pad, "note")
                if isinstance(note, int):
                    pads[note] = path
            state[key] = pads
        return state

    def _prime_drum_pads(self):
        self._mirror["drum_pads"] = self._drum_pad_map()

    def _diff_drum_pads(self):
        """Семпл, що зʼявився на паді -> SampleLoad.

        Окремий шлях, а не гілка _diff_devices, і на те є причина. Семпл на
        паді народжує НОВИЙ ланцюг, а нові контейнери дифф девайсів навмисно
        пропускає -- інакше копія треку сипала б подіями. Без цього правила
        партнер отримав би DeviceInsert на голий OriginalSimpler, тобто
        девайс без звуку. Перевірено на живому: сьогодні там тиша.
        """
        previous = self._mirror.get("drum_pads")
        if not previous:
            return
        current = self._drum_pad_map()
        for key, pads in current.items():
            was = previous.get(key)
            if was is None:
                continue  # новий рак: його вміст приїде знімком
            for note, path in sorted(pads.items()):
                if was.get(note) == path:
                    continue
                rel = self._sample_rel_path(path)
                if rel is None:
                    self._warn("семпл на паді %s лежить поза текою проєкту і не "
                               "синхронізується; File > Collect All and Save" % (note,))
                    continue
                track_id, kind, chain_ids, class_name, display_name, ordinal = key
                track_ref = {"id": track_id}
                if kind:
                    track_ref["kind"] = kind
                payload = {
                    "track": track_ref,
                    "target": {
                        "kind": "drum_pad",
                        "device": {"class_name": class_name,
                                   "class_display_name": display_name,
                                   "ordinal": ordinal},
                        "note": note,
                    },
                    "sample": {"path": rel, "name": rel.rsplit("/", 1)[-1]},
                }
                if chain_ids:
                    payload["target"]["chain_path"] = [{"id": cid} for cid in chain_ids]
                self._emit("SampleLoad", payload)

    def _apply_sample_to_pad(self, payload, target, item, gseq):
        """Семпл на пад Drum Rack. Прицілювання пряме -- перевірено на 12.3.5.

        Довідник LOM позначає selected_drum_pad як R, і це неправда: сеттер
        є, а семпл лягає САМЕ на виділений пад, а не на перший вільний.
        """
        note = target.get("note")
        if not isinstance(note, int) or not (0 <= note <= 127):
            self._warn("gseq %s: некоректна нота пада %r" % (gseq, note))
            return
        container, track = self._resolve_device_container(
            payload.get("track") or {}, target.get("chain_path"), gseq)
        if container is None:
            return
        device = None
        for candidate in (list(container.devices) if container is not None else []):
            if self._device_matches(candidate, target.get("device") or {}):
                device = candidate
                break
        if device is None:
            self._warn("gseq %s: Drum Rack не знайдено за адресою" % (gseq,))
            return
        try:
            pads = list(device.drum_pads)
        except Exception:
            self._warn("gseq %s: девайс за адресою -- не Drum Rack" % (gseq,))
            return
        pad = None
        for candidate in pads:
            if self._safe_attr(candidate, "note") == note:
                pad = candidate
                break
        if pad is None:
            self._warn("gseq %s: пада %s у раку немає" % (gseq, note))
            return
        if self._pad_sample_path(pad):
            return  # ідемпотентність: на паді вже щось лежить

        view = getattr(self._doc, "view", None)
        if view is None:
            return
        saved_track = self._safe_attr(view, "selected_track")
        struct_was = self._suppress_struct
        self._suppress_struct = True
        self._suppress_view = True
        self._view_applied_at = time.time()
        try:
            view.selected_track = track
            device.view.selected_drum_pad = pad
            Live.Application.get_application().browser.load_item(item)
        except Exception as e:
            self._warn("gseq %s: семпл не ліг на пад: %r" % (gseq, e))
        finally:
            try:
                if saved_track is not None:
                    view.selected_track = saved_track
            except Exception:
                pass
            self._view_applied_at = time.time()
            self._suppress_view = False
            self._mirror["view"] = self._view_signature()
            self._rewire_tracks()
            self._refresh_chains()
            self._persist_registry()
            self._prime_devices()
            self._prime_samples()
            self._prime_device_state()
            self._prime_drum_pads()
            self._suppress_struct = struct_was

    def _scan_samples(self):
        """Які семпли лежать поза текою проєкту і яких бракує локально.

        Live за замовчуванням не копіює семпл у проєкт -- .als тримає посилання
        на оригінал. Такий проєкт непереносимий: у партнера абсолютний шлях
        не існує, і Live покаже missing media. Collect All and Save через LOM
        не викликається, а clip.file_path доступний лише на читання, тож
        виправити це кодом не можна -- лише вчасно сказати.
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

        if op == "load_device":
            track, _idx = self._ai_target_track(action)
            ref = self._device_track_ref(track)
            if not ref:
                raise ValueError("track is not addressable")
            payload = {
                "track": ref,
                "item": {
                    "uri": action.get("uri"),
                    "name": action.get("name"),
                    "category": action.get("category", "audio_effects"),
                },
            }
            if action.get("index") is not None:
                payload["index"] = self._ai_int(action.get("index"), "index")
            self._queue_device_load(payload, "ai")
            return {"queued": True}

        if op == "lom_get":
            return self._ai_serialize(self._ai_resolve_path(action.get("path")))

        if op == "lom_dir":
            # Що обʼєкт узагалі показує. Потрібне для аудиту: у девайсів
            # половина цікавого лежить повз parameters (sample у Simpler,
            # chains у раках), а вгадувати імена по одному -- довго й ненадійно.
            # Лише читання: викликів звідси немає, значення не чіпаються.
            return self._ai_dir(self._ai_resolve_path(action.get("path")))

        if op == "lom_set":
            obj = self._ai_resolve_path(action.get("path"))
            prop = action.get("property", action.get("prop"))
            if not isinstance(prop, str) or not prop or prop.startswith("_"):
                raise ValueError("property must be a public string")
            # Половина цікавих властивостей приймає обʼєкт, а не число:
            # selected_drum_pad, selected_track, selected_chain. Через голий
            # JSON вони недосяжні, тож {"$path": [...]} резолвиться і тут.
            setattr(obj, prop, self._ai_arg(action.get("value")))
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
            args = [self._ai_arg(a) for a in args]
            kwargs = dict((k, self._ai_arg(v)) for k, v in kwargs.items())
            return self._ai_serialize(method(*args, **kwargs))

        raise ValueError("unknown op %r" % (op,))

    def _next_ai_seq(self):
        self._ai_seq += 1
        return "ai-%d" % self._ai_seq

    def _ai_target_track(self, action):
        ref = action.get("track")
        # Голе число -- це індекс треку. Раніше воно провалювалось крізь усі
        # гілки й мовчки бралось виділення: явний аргумент зникав безслідно,
        # а операція йшла не туди, куди просили.
        if isinstance(ref, int) and not isinstance(ref, bool):
            return self._track_by_index(ref)
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

    def _ai_arg(self, value):
        """Аргумент виклику. {"$path": [...]} -- посилання на обʼєкт LOM.

        Половина цікавих методів LOM приймає не числа, а самі обʼєкти
        (duplicate_clip_to_arrangement бере Clip), і через голий JSON вони
        недосяжні. Резолвер той самий, що для path, тож правила доступу
        не слабшають: приватні атрибути так само заборонені.
        """
        if isinstance(value, dict) and list(value.keys()) == ["$path"]:
            return self._ai_resolve_path(value["$path"])
        return value

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
        # Колекції LOM (tracks, scenes, clip_slots, devices, parameters) не є
        # ні list, ні tuple -- це власний Vector. Досі вони серіалізувались
        # як {"lom_type": "Vector"}, тобто перелічити треки через lom_get було
        # неможливо взагалі: ні пробі, ні планувальнику, ні людині.
        if self._is_lom_vector(value):
            items = []
            try:
                total = len(value)
            except Exception:
                total = 0
            for i in range(min(total, 64)):
                try:
                    items.append(self._ai_serialize(value[i], depth + 1))
                except Exception:
                    items.append(None)
            if total > 64:
                items.append({"truncated": total - 64})
            return items

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
        # Кілька скалярів, без яких обʼєкт непрозорий. CuePoint без time --
        # це просто назва: ні знайти його, ні порівняти з чужим неможливо.
        # Виявилось на пробі, яка не бачила локатора, що насправді доїхав.
        # WarpMarker без цих двох чисел -- порожня коробка: саме через це
        # жива перевірка порівнювала однакові {"lom_type": "WarpMarker"} і
        # рапортувала збіг, поки маркери насправді не застосовувались.
        for attr in ("time", "start_time", "end_time", "length",
                     "beat_time", "sample_time"):
            try:
                number = getattr(value, attr)
            except Exception:
                continue
            try:
                out[attr] = round(float(number), 6)
            except Exception:
                pass
        try:
            out.update(self._ai_parameter_summary(value))
        except Exception:
            pass
        return out

    @staticmethod
    def _is_lom_vector(value):
        """Чи це колекція LOM, яку можна перелічити.

        Перевіряємо поведінкою, а не назвою класу: у різних версіях Live
        вона зветься по-різному, а от len() і [i] є завжди. Рядки й словники
        сюди не потрапляють -- їх розбирають гілки вище.
        """
        if isinstance(value, (str, bytes, dict, list, tuple)):
            return False
        try:
            len(value)
        except Exception:
            return False
        try:
            if len(value):
                value[0]
        except Exception:
            return False
        return True

    def _ai_dir(self, obj):
        """Публічні атрибути обʼєкта, поділені на прості й обʼєктні.

        Прості (число, рядок, булеве) нас не дивують -- цікаве саме те, що
        віддає обʼєкт або колекцію: там ховається стан, якого немає серед
        parameters, і саме його ми досі не синхронізували.
        """
        out = {"lom_type": obj.__class__.__name__,
               "scalars": [], "objects": [], "collections": [], "callables": 0}
        for name in sorted(dir(obj)):
            if name.startswith("_"):
                continue
            try:
                value = getattr(obj, name)
            except Exception:
                continue
            if callable(value):
                out["callables"] += 1
                continue
            if value is None or isinstance(value, (bool, int, float, str)):
                out["scalars"].append(name)
                continue
            if self._is_lom_vector(value):
                try:
                    out["collections"].append("%s[%d]" % (name, len(value)))
                except Exception:
                    out["collections"].append(name)
                continue
            out["objects"].append("%s:%s" % (name, value.__class__.__name__))
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
        """Заповнює дзеркало поточним станом, щоб на старті не вистрелити пачкою подій."""
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
        """Заповнює дзеркало мікшера після бутстрапу реєстру.

        Без цього перший же рух будь-якого фейдера виглядав би як зміна відносно
        None і породжував подію -- а на старті таких «змін» одразу десятки.
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
            cross = self._crossfade_assign(track)
            if cross is not None:
                self._mirror["mix"][self._mix_key(track_ref, "crossfade_assign", None)] = cross
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
        # Дерево оновлюється тут же: так кожен наявний виклик _prime_devices
        # автоматично лишається точкою відліку для _diff_devices.
        self._mirror["device_tree"] = self._device_tree()
        # Базову лінію падів тримає одна функція, а не дубльований рядок
        self._prime_drum_pads()

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
                "timing": self._scene_timing(scene),
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
                "stop_off": self._state_stop_off(track, doc_scenes),
                "arrangement": self._state_arrangement(track),
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

        # Читаємо з LOM, а не з дзеркала: дзеркало -- це «що ми вже бачили»,
        # а знімок мусить бути «що є зараз».
        song = {}
        for prop in SONG_PROPS:
            value = self._song_prop_value(prop, self._safe_attr(self._doc, prop))
            if value is not None:
                song[prop] = value

        chains = []
        for rec in self._chain_records:
            uid = rec.get("id")
            chain = self._chains_reg.obj_of(uid) if uid else None
            if chain is None:
                continue
            entry = {"id": uid}
            entry.update(self._chain_state(chain))
            chains.append(entry)

        cues = [{"time": at, "name": name}
                for at, name in sorted(self._cue_map().items())]

        link = self._link_state()

        return {
            "version": STATE_VERSION,
            "script": SCRIPT_VERSION,
            "live": self._live_version(),
            "at": time.time(),
            "tempo": tempo,
            "playing": playing,
            "song": song,
            "link": link,
            "cues": cues,
            "chains": chains,
            "tracks": tracks,
            "aux_tracks": aux_tracks,
            "scenes": scenes,
        }

    def _link_state(self):
        """Стан Ableton Link. У знімок іде, у події -- НІ.

        Link -- налаштування машини, а не документа: він вирівнює темп і фазу
        долі між усіма, хто є в мережі, і вмикається на кожній машині окремо.
        Возити його подією означало б керувати чужим клоком, а це рівно те,
        чого ми не робимо (див. personal-not-shared у памʼяті).

        Але знати про розбіжність треба: коли в одного Link увімкнено, а в
        іншого ні, спільної долі немає, і квантований запуск кліпа спрацює
        в різні моменти. Тому стан у знімку є, і `diff` про нього скаже.
        """
        state = {}
        for prop, key in (("is_ableton_link_enabled", "enabled"),
                          ("is_ableton_link_start_stop_sync_enabled", "start_stop_sync")):
            value = self._safe_attr(self._doc, prop)
            if value is not None:
                state[key] = bool(value)
        return state or None

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
                entry = {"index": idx, "value": value}
                ret = self._send_return_ref(idx)
                if ret:
                    entry["return"] = ret
                mixer.setdefault("sends", []).append(entry)
            else:
                mixer[param] = value
        cross = self._crossfade_assign(track)
        if cross is not None:
            mixer["crossfade_assign"] = cross
        for prop in self._toggle_props(track):
            try:
                mixer[prop] = bool(getattr(track, prop))
            except Exception:
                pass
        return mixer

    def _state_devices(self, track):   # noqa: D401  (див. _sample_state нижче)
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
            sample = self._sample_state(device)
            if sample:
                entry["sample"] = sample
            block = self._device_state_block(device)
            if block:
                entry["state"] = block
            if chain_path:
                entry["chain_path"] = chain_path
            devices.append(entry)
        return devices

    def _state_stop_off(self, track, scenes):
        """Сцени, у яких слот цього треку БЕЗ стоп-кнопки.

        Перелічуємо саме вимкнені, бо ввімкнена -- стан Live за замовчуванням,
        і перелік усіх слотів сету означав би тисячі подій на дрібницю.
        Ціна асиметрії названа в PROTOCOL.md: знімок вирівнює лише вимкнені,
        а в обидва боки сходить SlotStopButtonSet під час самої сесії.
        """
        out = []
        try:
            slots = list(track.clip_slots)
        except Exception:
            return out
        for i, scene in enumerate(scenes):
            if i >= len(slots):
                break
            sid = self._scenes_reg.id_of(scene, create=False)
            if not sid:
                continue
            if self._stop_button_state(slots[i]) is False:
                out.append(sid)
        return out

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
            props = self._clip_props_state(clip)
            if props:
                entry["props"] = props
            markers = self._warp_markers(clip)
            if markers:
                entry["warp"] = markers
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

    # ----------------------------------------------------------- Arrangement
    #
    # Кліп в Arrangement не може носити власний id: Clip.set_data не існує
    # (перевірено на живому 12.3), рівно як і в сцени. Тож ідентичність живе
    # в мапі на Song, а локатором служить пара (uuid треку, start_time):
    # кліпи на одному треку не перекриваються, отже початок унікальний.
    #
    # Пересунути кліп прямо не можна -- Clip.start_time без сеттера. Переїзд
    # робиться як duplicate_clip_to_arrangement(сам кліп, нова позиція) плюс
    # delete_clip(старий); обидва виклики перевірені на живому Live.

    def _arr_clips(self, track):
        try:
            return list(track.arrangement_clips)
        except Exception:
            return []

    def _arr_start(self, clip):
        try:
            value = float(clip.start_time)
        except Exception:
            return None
        if not math.isfinite(value) or abs(value) > CLIP_LENGTH_MAX:
            return None
        return round(value, 6)

    def _prime_arrangement(self):
        """Видає uuid усім Arrangement-кліпам, спираючись на збережену мапу.

        Повертає True, якщо розкладка змінилась -- тоді її варто перезаписати
        в .als. Без цього переїзд кліпу лишив би в мапі стару позицію, і після
        відкриття файлу кліп дістав би новий uuid замість свого.
        """
        wanted = {}
        for rec in self._saved_arr_records:
            if rec.get("id"):
                wanted[(rec.get("track"), rec.get("start"))] = rec["id"]

        records = []
        for track in self._doc.tracks:
            track_ref = self._device_track_ref(track)
            if not track_ref:
                continue   # група або нерозділюваний трек -- як і всюди
            tid = track_ref.get("id")
            for clip in self._arr_clips(track):
                start = self._arr_start(clip)
                if start is None:
                    continue
                uid = self._arr_reg.id_of(clip, create=False)
                if not uid:
                    saved = wanted.get((tid, start))
                    if saved and not self._arr_reg.taken_by_other(saved, clip):
                        self._arr_reg.bind(saved, clip)
                        uid = saved
                    else:
                        uid = self._arr_reg.id_of(clip)
                records.append({"id": uid, "track": tid, "start": start,
                                "name": self._safe_name(clip)})

        changed = records != self._arr_records
        self._arr_records = records
        return changed

    # ------------------------------------------- Arrangement: події (стадія B)
    #
    # Форма подій задана обмеженнями LOM, виміряними на живому 12.3:
    #   створити -- лише duplicate_clip_to_arrangement(джерело, час);
    #   пересунути -- прямого сеттера немає, тож копія плюс видалення старої;
    #   видалити -- delete_clip(кліп).
    # Створити кліп "з нічого" не можна ніколи, тому приймальний бік збирає
    # тимчасове джерело в порожньому слоті Session і одразу його прибирає.

    def _resolve_arr_clip(self, payload):
        """Трек і Arrangement-кліп за uuid. (None, None) -- адреса не розвʼязалась."""
        track, _tref = self._resolve_device_track(payload.get("track") or {})
        if track is None:
            return None, None
        uid = (payload.get("clip") or {}).get("id")
        if not uid:
            return track, None
        return track, self._arr_reg.obj_of(uid)

    def _arr_time_from_payload(self, payload):
        try:
            value = float(payload.get("start_time"))
        except Exception:
            return None
        if not math.isfinite(value) or value < 0 or value > CLIP_LENGTH_MAX:
            return None
        return round(value, 6)

    def _arr_free_slot(self, track):
        """Порожній слот Session -- єдине місце, де можна зібрати джерело."""
        try:
            slots = list(track.clip_slots)
        except Exception:
            return None
        for slot in slots:
            try:
                if not slot.has_clip:
                    return slot
            except Exception:
                continue
        return None

    def _arr_place(self, track, source, start_time, gseq):
        try:
            return track.duplicate_clip_to_arrangement(source, float(start_time))
        except Exception as e:
            self._warn("gseq %s: кліп не ліг в Arrangement: %r" % (gseq, e))
            return None

    def _arr_after_write(self):
        self._suppress_struct = False
        self._rewire_tracks()
        self._prime_arrangement()
        self._persist_registry()

    def _apply_arr_create(self, payload, gseq):
        track, existing = self._resolve_arr_clip(payload)
        if track is None or existing is not None:
            return  # ідемпотентність: подія вже застосована
        meta = payload.get("clip") or {}
        uid = meta.get("id")
        start = self._arr_time_from_payload(payload)
        if not uid or start is None:
            return
        if not bool(meta.get("is_midi", True)):
            self._warn("gseq %s: audio-кліпи в Arrangement не створюємо" % (gseq,))
            return
        slot = self._arr_free_slot(track)
        if slot is None:
            self._warn("gseq %s: усі слоти Session зайняті, немає де зібрати "
                       "джерело для Arrangement" % (gseq,))
            return
        length = self._clip_length_from_payload(payload)
        self._suppress_struct = True
        try:
            slot.create_clip(length)
            placed = self._arr_place(track, slot.clip, start, gseq)
            if placed is not None:
                self._arr_reg.bind(uid, placed)
                # Назва й колір -- частина події, а не косметика: без них
                # копія приїжджає безіменною, і люди бачать різні лінійки.
                for prop in ("name", "color"):
                    if meta.get(prop) is None:
                        continue
                    try:
                        setattr(placed, prop, meta[prop])
                    except Exception as e:
                        self._warn("gseq %s: %s кліпу в Arrangement не встановився: %r"
                                   % (gseq, prop, e))
            try:
                slot.delete_clip()
            except Exception as e:
                self._warn("gseq %s: тимчасовий кліп не прибрався: %r" % (gseq, e))
        finally:
            self._arr_after_write()

    def _apply_arr_move(self, payload, gseq):
        track, clip = self._resolve_arr_clip(payload)
        if track is None or clip is None:
            return
        start = self._arr_time_from_payload(payload)
        if start is None or self._arr_start(clip) == start:
            return
        uid = (payload.get("clip") or {}).get("id")
        self._suppress_struct = True
        try:
            placed = self._arr_place(track, clip, start, gseq)
            if placed is None:
                return
            # Спершу привʼязка, потім видалення: якщо delete впаде, uuid уже
            # вказує на кліп, який реально лежить на новому місці.
            self._arr_reg.bind(uid, placed)
            try:
                track.delete_clip(clip)
            except Exception as e:
                self._warn("gseq %s: старий кліп не прибрався після переїзду: %r"
                           % (gseq, e))
        finally:
            self._arr_after_write()

    def _apply_arr_delete(self, payload, gseq):
        track, clip = self._resolve_arr_clip(payload)
        if track is None or clip is None:
            return
        self._suppress_struct = True
        try:
            track.delete_clip(clip)
        except Exception as e:
            self._warn("gseq %s: кліп не видалився з Arrangement: %r" % (gseq, e))
        finally:
            self._arr_after_write()

    def _apply_arr_notes(self, payload, gseq):
        _track, clip = self._resolve_arr_clip(payload)
        if clip is None:
            return
        validated = self._validated_note_region(payload)
        if validated is None:
            self._warn("gseq %s: некоректний нотний регіон для Arrangement" % (gseq,))
            return
        region, target = validated
        specs = tuple(self._make_note_spec(note) for note in target)
        try:
            clip.remove_notes_extended(region[0], region[1], region[2], region[3])
            if specs:
                clip.add_new_notes(specs)
        except Exception as e:
            self._warn("gseq %s: ноти в Arrangement не лягли: %r" % (gseq, e))

    def _emit_arr_create(self, track_ref, clip, uid, start):
        meta = {"id": uid}
        try:
            meta["length"] = round(float(clip.length), 6)
        except Exception:
            meta["length"] = NOTE_TIME_SPAN
        name = self._safe_name(clip)
        if name:
            meta["name"] = name
        color = self._safe_color(clip)
        if color is not None:
            meta["color"] = color
        try:
            meta["is_midi"] = bool(clip.is_midi_clip)
        except Exception:
            meta["is_midi"] = True
        if not meta["is_midi"]:
            # Audio-кліп несе не структуру, а семпл: партнер не може
            # створити його з нічого, зате може завантажити той самий файл.
            # ArrangementClipCreate тут був би подією, яку приймальний бік
            # чесно відхилить -- тож її й не шлемо.
            path = self._safe_attr(clip, "file_path")
            rel = self._sample_rel_path(str(path) if path else "")
            if rel is None:
                self._warn("audio-кліп в Arrangement із семплом поза текою "
                           "проєкту не синхронізується; File > Collect All and Save")
                return
            self._emit("SampleLoad", {
                "track": track_ref,
                "clip": {"id": uid},
                "target": {"kind": "arrangement", "start_time": start},
                "sample": {"path": rel, "name": rel.rsplit("/", 1)[-1]},
            })
            return
        self._emit("ArrangementClipCreate",
                   {"track": track_ref, "clip": meta, "start_time": start})
        # Вміст -- окремими подіями: створення має лишатись маленьким, інакше
        # один довгий кліп заблокував би чергу партнера на секунди.
        notes = self._clip_notes(clip)
        if not notes:
            return
        for region, part in self._note_regions_for({"length": meta["length"]}, notes):
            # region тут -- уже готовий dict. Розпакувати його в чотири імені
            # не можна: розпакування словника дає КЛЮЧІ, і в payload летіли б
            # рядки "from_pitch" замість чисел.
            self._emit("ArrangementClipNotesSet", {
                "track": track_ref,
                "clip": {"id": uid},
                "region": region,
                "notes": part,
            })

    def _diff_arrangement(self):
        """Події з різниці лінійок. Викликається ДО _prime_arrangement.

        Переїзд розпізнається за тим, що вижив сам обʼєкт Clip: uuid той
        самий, змінився лише start_time. Якщо Live обʼєкт перестворив, вийде
        пара видалення+створення -- партнер усе одно збіжиться, просто кліп
        дістане новий uuid і ноти поїдуть заново.
        """
        previous = dict((rec["id"], rec) for rec in self._arr_records)
        seen = set()
        for track in self._doc.tracks:
            track_ref = self._device_track_ref(track)
            if not track_ref:
                continue
            for clip in self._arr_clips(track):
                start = self._arr_start(clip)
                if start is None:
                    continue
                uid = self._arr_reg.id_of(clip, create=False)
                if uid and uid in previous:
                    seen.add(uid)
                    if previous[uid].get("start") != start:
                        self._emit("ArrangementClipMove", {
                            "track": track_ref, "clip": {"id": uid},
                            "start_time": start})
                    continue
                if not uid:
                    uid = self._arr_reg.id_of(clip)
                seen.add(uid)
                self._emit_arr_create(track_ref, clip, uid, start)
        for uid, rec in previous.items():
            if uid in seen:
                continue
            self._emit("ArrangementClipDelete",
                       {"track": {"id": rec.get("track")}, "clip": {"id": uid}})

    def _state_arrangement(self, track):
        """Arrangement-кліпи треку для знімка."""
        entries = []
        track_ref = self._device_track_ref(track)
        if not track_ref:
            return entries
        for clip in self._arr_clips(track):
            start = self._arr_start(clip)
            uid = self._arr_reg.id_of(clip, create=False)
            if start is None or not uid:
                continue
            entry = {"id": uid, "start_time": start}
            for prop, cast in (("end_time", float), ("length", float),
                               ("name", None), ("color", int)):
                try:
                    value = getattr(clip, prop)
                    entry[prop] = round(float(value), 6) if cast is float else (
                        int(value) if cast is int else self._doc_str(value))
                except Exception:
                    pass
            try:
                entry["is_midi"] = bool(clip.is_midi_clip)
            except Exception:
                pass
            props = self._clip_props_state(clip)
            if props:
                entry["props"] = props
            markers = self._warp_markers(clip)
            if markers:
                entry["warp"] = markers
            loop = self._clip_loop_state(clip)
            if loop:
                entry["loop"] = loop
            try:
                if clip.is_midi_clip:
                    entry["notes"] = self._clip_notes(clip)
            except Exception:
                pass
            entries.append(entry)
        return entries

    def _on_arrangement(self):
        """Структура Arrangement змінилась.

        uuid видається до будь-якої емісії: без нього кліп нічим адресувати,
        а _diff_arrangement саме за uuid відрізняє переїзд від перестворення.
        """
        if not self._registry_ready:
            return
        if not self._suppress_struct:
            self._safe(self._diff_arrangement)
        changed = self._prime_arrangement()
        # Новий кліп у лінійці ще не має жодної підписки: без перепідключення
        # його ноти й властивості не поїхали б доти, доки щось інше не смикне
        # _rewire_tracks. Перепідписуємось цілком, бо часткове вішання
        # накопичило б дублі -- кожен виклик робить НОВІ callback-обʼєкти.
        self._rewire_tracks()
        self._prime_arrangement_clips()
        if changed:
            self._persist_registry()

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
                "track": self._safe_name(local) or track.get("name"),
                "name": (theirs or mine or {}).get("name"),
                "here": bool(mine),
            })
            if len(gaps) >= MISSING_LIMIT:
                break

        gaps.extend(self._arrangement_gaps(state, MISSING_LIMIT - len(gaps)))
        return gaps[:MISSING_LIMIT]

    def _arrangement_gaps(self, state, budget):
        """Чим різняться Arrangement у нас і в партнера.

        Події для лінійки є, але знімок структури в ній не будує -- тобто
        розбіжність, що вже сталася, знімком не лікується, лише руками.
        Саме тому її треба назвати вголос: мовчазний розсинхрон тут
        найгірший, бо в Session видно порожній слот, а лінійку партнера
        не видно взагалі.
        """
        gaps = []
        if budget <= 0:
            return gaps
        for track in (state.get("tracks") or []):
            uid = track.get("id")
            if not uid:
                continue
            local, _ref = self._resolve_device_track({"id": uid})
            if local is None:
                continue
            theirs = dict((c.get("id"), c) for c in (track.get("arrangement") or [])
                          if c.get("id"))
            mine = dict((c.get("id"), c) for c in self._state_arrangement(local))
            name = track.get("name") or self._safe_name(local)
            for cid in sorted(set(theirs) - set(mine)):
                gaps.append({"what": "arrangement", "track": name, "here": False,
                             "start": theirs[cid].get("start_time"),
                             "name": theirs[cid].get("name")})
            for cid in sorted(set(mine) - set(theirs)):
                gaps.append({"what": "arrangement", "track": name, "here": True,
                             "start": mine[cid].get("start_time"),
                             "name": mine[cid].get("name")})
            # Той самий кліп на різних позиціях -- переїзд, якого ми не бачили
            for cid in sorted(set(mine) & set(theirs)):
                if theirs[cid].get("start_time") != mine[cid].get("start_time"):
                    gaps.append({"what": "arrangement", "track": name, "here": None,
                                 "start": theirs[cid].get("start_time"),
                                 "mine": mine[cid].get("start_time"),
                                 "name": mine[cid].get("name")})
            if len(gaps) >= budget:
                break
        return gaps[:budget]

    # ------------------------------------------------- завантаження девайсів

    def _browser_index(self):
        """uri -> BrowserItem для стокових девайсів. Будується раз на сесію.

        Лише діти ПЕРШОГО рівня трьох категорій -- і це не спрощення, а межа,
        задана вимірюванням: дампи з двох машин показали, що audio_effects
        і instruments мають ідентичні uri виду query:AudioFx#Compressor,
        а вміст (drums, пресети) адресується локальними FileId, які на кожній
        машині свої. Тож глибше лізти немає сенсу, а обхід дерева не потрібен.
        """
        if self._browser_cache is not None:
            return self._browser_cache
        index = {}
        try:
            browser = Live.Application.get_application().browser
        except Exception:
            self._browser_cache = index
            return index
        for category in BROWSER_CATEGORIES:
            by_uri, by_name = {}, {}
            try:
                children = list(getattr(browser, category).children)
            except Exception:
                children = []
            for item in children:
                try:
                    if not item.is_device or not item.is_loadable:
                        continue
                    uri = str(item.uri)
                    name = str(item.name)
                except Exception:
                    continue
                if uri:
                    by_uri[uri] = item
                if name:
                    by_name.setdefault(name.lower(), item)
            index[category] = (by_uri, by_name)
        self._browser_cache = index
        return index

    def _browser_item(self, ref):
        """Айтем за uri; за відсутності -- за назвою в ТІЙ САМІЙ категорії.

        uri може зсунутись між версіями Live, а назва -- те, що впізнає людина.
        Міжкатегорійний збіг не приймаємо: Compressor у ефектах і однойменний
        пресет деінде -- різні речі.
        """
        category = ref.get("category")
        if category not in BROWSER_CATEGORIES:
            return None
        by_uri, by_name = self._browser_index().get(category, ({}, {}))
        item = by_uri.get(ref.get("uri"))
        if item is not None:
            return item
        name = ref.get("name")
        return by_name.get(name.lower()) if isinstance(name, str) else None

    def _device_is_bare(self, device):
        """Девайс щойно з браузера, без власного вмісту.

        Пресет Compressor/Warm Bus має той самий class_name, що й голий
        Compressor, тож без цієї перевірки автоемісія тихо віддала б партнеру
        дефолт замість того, що чує автор. Різниця видна в name: у стокового
        девайса воно дорівнює class_display_name, у пресета -- назві пресета.
        Рак із ланцюгами і Drum Rack із падами -- теж власний вміст.
        """
        try:
            if str(device.name) != str(device.class_display_name):
                return False
        except Exception:
            return False
        if self._device_has_chains(device):
            for _kind, chains in self._rack_chain_groups(device):
                if chains:
                    return False
        try:
            for pad in device.drum_pads:
                if pad.chains:
                    return False
        except Exception:
            pass
        return True

    @staticmethod
    def _device_tree_sig(device):
        """Сигнатура для діффу структури -- разом із name.

        Без name пресет Compressor/Warm Bus не відрізнити від голого
        Compressor поруч, і вставку зарахувало б сусідові: партнер дістав
        би другий примірник того, що в нього вже є.
        """
        try:
            return (str(device.class_name), str(device.class_display_name),
                    str(device.name))
        except Exception:
            return None

    def _device_tree(self):
        """Знімок структури: контейнер -> сигнатури девайсів у порядку.

        Порожній контейнер мусить мати запис, і це не дрібниця. _diff_devices
        трактує відсутній ключ як «новий контейнер» і мовчки пропускає його --
        інакше копія треку сипала б подіями на кожен свій девайс. Тож якби
        дерево будувалось із ітератора девайсів, трек без девайсів не мав би
        запису взагалі, і ПЕРШИЙ девайс на ньому не породжував би події ніколи.
        Тому обхід тут по контейнерах, а не по девайсах.
        """
        tree = {}
        for track in self._iter_device_tracks():
            track_ref = self._device_track_ref(track)
            if not track_ref:
                continue
            base = (track_ref.get("id"), track_ref.get("kind"))

            def walk(container, chain_path, depth, base=base):
                if depth > 16:
                    return
                sigs = []
                try:
                    devices = list(container.devices)
                except Exception:
                    devices = []
                for device in devices:
                    if self._device_signature(device) is None:
                        continue
                    sigs.append(self._device_tree_sig(device))
                    if not self._device_has_chains(device):
                        continue
                    for _kind, chains in self._rack_chain_groups(device):
                        for chain in chains:
                            cid = self._chains_reg.id_of(chain, create=False)
                            if cid:
                                walk(chain, chain_path + [{"id": cid}], depth + 1)
                tree[(base, tuple(c.get("id") for c in chain_path))] = sigs

            walk(track, [], 0)
        return tree

    @staticmethod
    def _single_change_spot(short, long_):
        """Індекс, за яким long_ відрізняється від short рівно одним елементом.

        None -- відмінність не одинична, тобто в контейнері сталось щось
        складніше за одну вставку чи одне видалення.
        """
        for i in range(len(long_)):
            if short[:i] == long_[:i] and short[i:] == long_[i + 1:]:
                return i
        return None

    def _diff_devices(self):
        """Структурна зміна дерева девайсів -> DeviceInsert/Delete/Move.

        Обережність тут і далі дорожча за повноту, але межа зсунулась. Раніше
        видалення події не мало взагалі, тож переїзд девайса виглядав як пара
        "зникло там, зʼявилось тут" і глушив емісію цілком. Тепер обидві
        половини цієї пари мають імена, і пара зі збіжною сигнатурою
        розпізнається як переїзд.

        Що досі глушить емісію: будь-яка зміна складніша за одну вставку, одне
        видалення або один переїзд. Дві появи, перестановка трьох девайсів,
        видалення разом зі вставкою іншого -- усе це лишається дірою, яку
        закриє знімок. Мовчки покладений не той девайс гірший за діру.
        """
        previous = self._mirror.get("device_tree")
        if not previous:
            return
        current = self._device_tree()
        added = []
        removed = []
        for key, now in current.items():
            was = previous.get(key)
            if was is None:
                continue          # новий контейнер: копія треку або свіжий ланцюг
            if now == was:
                continue
            if len(now) == len(was) + 1:
                spot = self._single_change_spot(was, now)
                if spot is None:
                    return
                added.append((key, spot, now[spot]))
            elif len(now) + 1 == len(was):
                spot = self._single_change_spot(now, was)
                if spot is None:
                    return
                removed.append((key, spot, was[spot]))
            else:
                return

        if len(added) == 1 and len(removed) == 1 and added[0][2] == removed[0][2]:
            self._emit_device_move(removed[0], added[0])
        elif len(added) == 1 and not removed:
            self._emit_device_insert(added[0])
        elif len(removed) == 1 and not added:
            self._emit_device_delete(removed[0])

    def _key_address(self, key):
        """Ключ дерева -> адреса в події. None, якщо трек більше не спільний."""
        (tid, kind), chain_ids = key
        if not tid:
            return None, None
        if tid in self._unshared_tracks:
            return None, None
        track_ref = {"id": tid}
        if kind:
            track_ref["kind"] = kind
        return track_ref, [{"id": cid} for cid in chain_ids]

    def _live_device_at(self, key, spot):
        """Живий обʼєкт девайса за ключем дерева -- потрібен там, де сигнатури мало."""
        (tid, kind), chain_ids = key
        for track in self._iter_device_tracks():
            track_ref = self._device_track_ref(track)
            if not track_ref or (track_ref.get("id"), track_ref.get("kind")) != (tid, kind):
                continue
            seen = {}
            for _container, device, chain_path in self._iter_track_devices(track):
                ckey = tuple(c.get("id") for c in chain_path)
                idx = seen.get(ckey, 0)
                seen[ckey] = idx + 1
                if ckey == chain_ids and idx == spot:
                    return device
            return None
        return None

    @staticmethod
    def _sig_payload(sig):
        class_name, display_name, name = sig
        return {"class_name": class_name, "class_display_name": display_name,
                "name": name}

    def _emit_device_insert(self, entry):
        key, spot, sig = entry
        track_ref, chain_path = self._key_address(key)
        if track_ref is None:
            return
        # Фільтр на голий девайс лишається: insert_device уміє лише стокову
        # назву, тож пресет Compressor/Warm Bus приїхав би партнеру дефолтом.
        device = self._live_device_at(key, spot)
        if device is None or not self._device_is_bare(device):
            return
        payload = {"track": track_ref, "index": spot,
                   "device": {"class_display_name": sig[1]}}
        if chain_path:
            payload["chain_path"] = chain_path
        self._emit("DeviceInsert", payload)

    def _emit_device_delete(self, entry):
        key, spot, sig = entry
        track_ref, chain_path = self._key_address(key)
        if track_ref is None:
            return
        # На відміну від вставки, тут голизна не потрібна: видалити за індексом
        # можна що завгодно, і пресет теж. Сигнатура їде для звірки на прийомі.
        payload = {"track": track_ref, "index": spot,
                   "device": self._sig_payload(sig)}
        if chain_path:
            payload["chain_path"] = chain_path
        self._emit("DeviceDelete", payload)

    def _emit_device_move(self, gone, appeared):
        if DEVICE_MOVE_MODE == "pair":
            # Пара коштує партнеру значень параметрів: девайс перестворюється
            # з дефолтів (виміряно на 12.3.8 -- Frequency 0.25 -> 0.899657).
            # І якщо девайс не голий, вставка неможлива взагалі, тож саме лише
            # видалення знищило б партнеру девайс без заміни. Тоді мовчимо.
            device = self._live_device_at(appeared[0], appeared[1])
            if device is None or not self._device_is_bare(device):
                return
            self._emit_device_delete(gone)
            self._emit_device_insert(appeared)
            return

        src_ref, src_chain = self._key_address(gone[0])
        dst_ref, dst_chain = self._key_address(appeared[0])
        if src_ref is None or dst_ref is None:
            return
        source = {"track": src_ref, "index": gone[1]}
        if src_chain:
            source["chain_path"] = src_chain
        target = {"track": dst_ref, "index": appeared[1]}
        if dst_chain:
            target["chain_path"] = dst_chain
        self._emit("DeviceMove", {"from": source, "to": target,
                                  "device": self._sig_payload(gone[2])})

    def _queue_device_load(self, payload, gseq):
        self._load_queue.append({"payload": payload, "gseq": gseq, "since": time.time()})

    def _queue_cue(self, etype, payload, gseq):
        """Локатор чекає на зупинку транспорту.

        set_or_delete_cue працює лише в поточній позиції, тож поставити
        локатор -- це на мить зрушити плейхед. Під час відтворення це чути,
        і чужий локатор не варта того причина, щоб перебити людині гру.
        """
        self._load_queue.append({"etype": etype, "payload": payload,
                                 "gseq": gseq, "since": time.time()})

    def _queue_device_struct(self, etype, payload, gseq):
        self._load_queue.append({"etype": etype, "payload": payload,
                                 "gseq": gseq, "since": time.time()})

    def _flush_device_loads(self):
        """Один девайс за тік: завантаження важкого інструмента блокує на
        сотні мілісекунд, і залп підвісив би Live."""
        if not self._load_queue:
            return
        # Голова черги може чекати -- локатор до паузи транспорту, семпл
        # до приїзду файлу. Тримати за нею всю чергу не можна: одна подія,
        # що чекає пʼять хвилин, зупинила б і завантаження девайсів. Тому
        # той, хто не може зараз, іде в кінець, а ми пробуємо наступного.
        for _ in range(len(self._load_queue)):
            if self._flush_one():
                return
            self._load_queue.append(self._load_queue.pop(0))

    def _flush_one(self):
        """True -- щось зроблено або відкинуто; False -- голова ще чекає."""
        entry = self._load_queue[0]
        etype = entry.get("etype", "DeviceLoad")
        # Чекає на зупинку транспорту лише DeviceLoad: тільки він рухає виділення.
        # insert_device і move_device беруть індекс явно, тож під запис безпечні.
        if etype in ("CueSet", "CueDelete"):
            if self._safe_attr(self._doc, "is_playing"):
                if time.time() - entry["since"] < CUE_QUEUE_MAX_SEC:
                    return False  # чекаємо, доки транспорт зупиниться
                self._load_queue.pop(0)
                self._warn("gseq %s: локатор не поставлено -- відтворення "
                           "триває надто довго" % (entry["gseq"],))
                return True
            handler = (self._apply_cue_set if etype == "CueSet"
                       else self._apply_cue_delete)
            # Знімаємо з черги ЛИШЕ коли зроблено: першим тіком локатор
            # просить плейхед зрушити, і зробити сам перемикач може аж
            # наступним -- запис current_song_time не миттєвий.
            done = self._safe(handler, entry["payload"], entry["gseq"])
            if done:
                self._load_queue.pop(0)
                return True
            if time.time() - entry["since"] < CUE_QUEUE_MAX_SEC:
                return False
            self._load_queue.pop(0)
            self._warn("gseq %s: локатор не поставлено -- плейхед не став "
                       "на місце" % (entry["gseq"],))
            return True
        if etype in ("DeviceLoad", "SampleLoad") and self._recording_guard():
            if time.time() - entry["since"] < LOAD_QUEUE_MAX_SEC:
                return False  # чекаємо, доки транспорт зупиниться
            self._load_queue.pop(0)
            self._warn("gseq %s: девайс не завантажено -- запис триває надто довго"
                       % (entry["gseq"],))
            return True
        if etype == "SampleLoad":
            # Єдиний тип, який може чесно сказати "ще не готовий": файл
            # їде filesync-ом окремо від події, і чекати його -- нормально.
            # З черги знімаємо ЛИШЕ коли зробили: інакше ротація викликача
            # прокрутила б не того, кого щойно пробували.
            done = self._safe(self._apply_sample_load, entry["payload"], entry["gseq"])
            if done:
                self._load_queue.pop(0)
                return True
            if time.time() - entry["since"] < SAMPLE_QUEUE_MAX_SEC:
                return False  # файл ще їде -- хай інші йдуть попереду
            self._load_queue.pop(0)
            self._warn("gseq %s: семпл %r так і не зʼявився в теці проєкту"
                       % (entry["gseq"],
                          ((entry["payload"] or {}).get("sample") or {}).get("path")))
            return True
        self._load_queue.pop(0)
        handler = {"DeviceLoad": self._load_device,
                   "DeviceInsert": self._apply_device_insert,
                   "DeviceMove": self._apply_device_move}.get(etype)
        if handler is not None:
            self._safe(handler, entry["payload"], entry["gseq"])
        return True

    def _resolve_device_container(self, ref, chain_path, gseq):
        """Трек + ланцюг -> контейнер, у якому живуть девайси.

        Той самий шлях, що в _load_device, але окремо: три структурні події
        адресують контейнер однаково, і розбіжність тут коштувала б девайса,
        покладеного не туди.
        """
        track, _tref = self._resolve_device_track(ref or {})
        if track is None:
            return None, None
        container = track
        for chain_ref in (chain_path or []):
            chain = self._chains_reg.obj_of((chain_ref or {}).get("id"))
            if chain is None or not self._chain_belongs_to(container, chain):
                self._warn("gseq %s: ланцюг не резолвиться" % (gseq,))
                return None, None
            container = chain
        return container, track

    def _device_at(self, container, index):
        try:
            devices = list(container.devices)
        except Exception:
            return None
        if not isinstance(index, int) or index < 0 or index >= len(devices):
            return None
        return devices[index]

    def _device_matches(self, device, ref):
        """Звірка сигнатури перед руйнівною дією.

        Індекс каже, що поїхало; сигнатура ловить те, що поїхало не те. Без неї
        розбіжність станів стерла б партнеру чужий девайс мовчки.
        """
        if not isinstance(ref, dict):
            return True
        signature = self._device_signature(device)
        if signature is None:
            return False
        class_name, display_name = signature
        if ref.get("class_name") and ref["class_name"] != class_name:
            return False
        if ref.get("class_display_name") and ref["class_display_name"] != display_name:
            return False
        name = ref.get("name")
        if name is not None:
            try:
                if str(device.name) != name:
                    return False
            except Exception:
                return False
        return True

    def _after_struct_change(self):
        self._rewire_tracks()
        self._refresh_chains()
        self._persist_registry()
        self._prime_devices()
        self._prime_samples()
        self._prime_device_state()

    def _apply_device_insert(self, payload, gseq):
        container, _track = self._resolve_device_container(
            payload.get("track"), payload.get("chain_path"), gseq)
        if container is None:
            return
        ref = payload.get("device") or {}
        name = ref.get("class_display_name")
        if not name:
            self._warn("gseq %s: DeviceInsert без class_display_name" % (gseq,))
            return
        index = payload.get("index")
        if not isinstance(index, int) or index < 0:
            index = -1
        was = self._suppress_struct
        self._suppress_struct = True
        try:
            container.insert_device(str(name), index)
        except Exception as e:
            self._warn("gseq %s: %s" % (gseq, self._insert_failure(name, e)))
        finally:
            self._after_struct_change()
            self._suppress_struct = was

    @staticmethod
    def _insert_failure(name, error):
        """Дві відмови insert_device означають різне, і партнеру треба різне.

        "not available" -- назву Live знає, але девайса немає в цій редакції чи
        бібліотеці. "not found" -- назви не знає взагалі: стороннє, пресет,
        друкарська помилка. Перше лікується докупкою, друге -- ні.
        """
        text = str(error)
        if "not available" in text:
            return ("девайс %r є в Live, але недоступний у твоїй редакції "
                    "чи бібліотеці" % (name,))
        if "not found" in text:
            return "девайс %r твоя інсталяція не знає" % (name,)
        return "девайс %r не вставився: %s" % (name, text)

    def _apply_device_delete(self, payload, gseq):
        container, _track = self._resolve_device_container(
            payload.get("track"), payload.get("chain_path"), gseq)
        if container is None:
            return
        index = payload.get("index")
        device = self._device_at(container, index)
        if device is None:
            self._warn("gseq %s: девайса за індексом %r немає" % (gseq, index))
            return
        if not self._device_matches(device, payload.get("device")):
            self._warn("gseq %s: за індексом %r стоїть інший девайс -- не видаляю"
                       % (gseq, index))
            return
        was = self._suppress_struct
        self._suppress_struct = True
        try:
            container.delete_device(index)
        except Exception as e:
            self._warn("gseq %s: девайс не видалився: %r" % (gseq, e))
        finally:
            self._after_struct_change()
            self._suppress_struct = was

    def _apply_device_move(self, payload, gseq):
        source = payload.get("from") or {}
        target = payload.get("to") or {}
        src_container, _src_track = self._resolve_device_container(
            source.get("track"), source.get("chain_path"), gseq)
        if src_container is None:
            return
        dst_container, _dst_track = self._resolve_device_container(
            target.get("track"), target.get("chain_path"), gseq)
        if dst_container is None:
            return
        device = self._device_at(src_container, source.get("index"))
        if device is None:
            self._warn("gseq %s: девайса для переїзду за індексом %r немає"
                       % (gseq, source.get("index")))
            return
        if not self._device_matches(device, payload.get("device")):
            self._warn("gseq %s: за індексом %r стоїть інший девайс -- не переношу"
                       % (gseq, source.get("index")))
            return
        index = target.get("index")
        if not isinstance(index, int) or index < 0:
            index = 0
        # Live не клампить сам: завелика позиція дає RuntimeError
        # "target_index out of range" (виміряно на 12.3.8), і девайс лишається
        # на місці. Розбіжність станів тут імовірніша за помилку відправника,
        # тож кладемо в кінець, а не втрачаємо подію.
        try:
            ceiling = len(list(dst_container.devices))
        except Exception:
            ceiling = index
        if index > ceiling:
            index = ceiling
        was = self._suppress_struct
        self._suppress_struct = True
        try:
            self._doc.move_device(device, dst_container, index)
        except Exception as e:
            self._warn("gseq %s: девайс не переїхав: %r" % (gseq, e))
        finally:
            self._after_struct_change()
            self._suppress_struct = was

    def _load_device(self, payload, gseq):
        ref = payload.get("item") or {}
        item = self._browser_item(ref)
        if item is None:
            self._warn("gseq %s: у твоїй бібліотеці немає %r"
                       % (gseq, ref.get("name") or ref.get("uri")))
            return
        track, _tref = self._resolve_device_track(payload.get("track") or {})
        if track is None:
            return
        container = track
        for chain_ref in (payload.get("chain_path") or []):
            chain = self._chains_reg.obj_of((chain_ref or {}).get("id"))
            if chain is None or not self._chain_belongs_to(container, chain):
                self._warn("gseq %s: ланцюг для завантаження не резолвиться" % (gseq,))
                return
            container = chain

        view = getattr(self._doc, "view", None)
        if view is None:
            return
        # load_item кладе девайс у ВИДІЛЕНИЙ обʼєкт, тож вид доводиться рухати.
        # Механізм глушіння той самий, що у follow: інакше власний рух виділення
        # полетів би партнеру як присутність.
        saved_track = self._safe_attr(view, "selected_track")
        saved_scene = self._safe_attr(view, "selected_scene")
        # Без цього власне завантаження повернулось би партнеру автоемісією.
        struct_was = self._suppress_struct
        self._suppress_struct = True
        self._suppress_view = True
        self._view_applied_at = time.time()
        try:
            view.selected_track = track
            if container is not track:
                try:
                    view.selected_chain = container
                except Exception:
                    pass  # у старіших збірках вибір ланцюга недоступний
            index = payload.get("index")
            try:
                devices = list(container.devices)
                if isinstance(index, int) and 0 < index <= len(devices):
                    track.view.selected_device = devices[index - 1]
            except Exception:
                pass
            Live.Application.get_application().browser.load_item(item)
        finally:
            try:
                if saved_track is not None:
                    view.selected_track = saved_track
                if saved_scene is not None:
                    view.selected_scene = saved_scene
            except Exception:
                pass
            self._view_applied_at = time.time()
            self._suppress_view = False
            self._mirror["view"] = self._view_signature()
            self._rewire_tracks()
            self._refresh_chains()
            self._persist_registry()
            self._prime_devices()
            self._prime_samples()
            self._prime_device_state()
            self._suppress_struct = struct_was

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

        if etype == "DeviceLoad":
            ref = payload.get("item") or {}
            if self._browser_item(ref) is None:
                return {"what": "device_item",
                        "name": ref.get("name"), "uri": ref.get("uri")}
            return None

        if etype == "DeviceStateSet":
            device = self._resolve_device_only(track, payload.get("chain_path"),
                                               payload.get("device"))
            display = (payload.get("device") or {}).get("class_display_name")
            if device is None:
                return {"what": "device", "track": self._safe_name(track), "device": display}
            if not hasattr(device, payload.get("prop") or ""):
                return {"what": "device_state", "track": self._safe_name(track),
                        "device": display, "name": payload.get("prop")}
            return None

        if etype == "SamplePropSet":
            device = self._resolve_device_only(track, payload.get("chain_path"),
                                               payload.get("device"))
            display = (payload.get("device") or {}).get("class_display_name")
            if device is None:
                return {"what": "device", "track": self._safe_name(track), "device": display}
            if self._sample_of(device) is None:
                return {"what": "sample", "track": self._safe_name(track), "device": display}
            return None

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

        # Ланцюг і кліп у лінійці адресуються власним uuid, повз сцену.
        # Без цих двох гілок пропущене рахувалось би застосованим: _apply
        # на нерозвʼязаний uuid мовчки виходить, і звіт про це не дізнався б.
        chain_ref = payload.get("chain")
        if isinstance(chain_ref, dict) and chain_ref.get("id"):
            if self._chains_reg.obj_of(chain_ref["id"]) is None:
                return {"what": "chain", "id": chain_ref["id"]}
            return None

        clip_ref = payload.get("clip")
        if isinstance(clip_ref, dict) and clip_ref.get("id"):
            _track, clip = self._resolve_arr_clip(payload)
            if clip is None:
                return {"what": "arr_clip", "id": clip_ref["id"],
                        "track": self._safe_name(track)}
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
        # Розмір такту й тональність -- частина документа, а не смак: без них
        # ті самі позиції нот означають у партнера інше.
        for prop, value in (state.get("song") or {}).items():
            ops.append(("SongPropSet", {"prop": prop, "value": value}))

        for track in state.get("tracks") or []:
            ref = {"id": track.get("id")}
            if not ref["id"]:
                continue
            ops.extend(self._meta_ops("track", ref, track))
            ops.extend(self._mixer_ops(ref, track.get("mixer") or {}))
            ops.extend(self._device_ops(ref, track.get("devices") or []))
            ops.extend(self._clip_ops(ref, track.get("clips") or []))
            for sid in track.get("stop_off") or []:
                ops.append(("SlotStopButtonSet",
                            {"track": ref, "scene": {"id": sid}, "value": False}))

        for aux in state.get("aux_tracks") or []:
            ref = {"id": aux.get("id"), "kind": aux.get("kind")}
            if not ref["id"] or not ref["kind"]:
                continue
            ops.extend(self._meta_ops("track", ref, aux))
            ops.extend(self._mixer_ops(ref, aux.get("mixer") or {}))
            ops.extend(self._device_ops(ref, aux.get("devices") or []))

        # Мікшер ланцюгів: у Drum Rack це гучність кожного пада.
        for chain in state.get("chains") or []:
            cid = (chain or {}).get("id")
            if not cid:
                continue
            for param in ("volume", "panning", "mute", "solo"):
                if chain.get(param) is None:
                    continue
                ops.append(("ChainMixerSet", {"chain": {"id": cid},
                                              "param": param, "value": chain[param]}))
            for prop in ("name", "color"):
                if chain.get(prop) is None:
                    continue
                ops.append(("ObjectMetaSet", {"object": "chain", "chain": {"id": cid},
                                              "prop": prop, "value": chain[prop]}))

        # Локатори -- структура документа: «Verse», «Drop». Партнер без них
        # бачить голу лінійку.
        for cue in state.get("cues") or []:
            at = (cue or {}).get("time")
            if isinstance(at, bool) or not isinstance(at, (int, float)):
                continue
            ops.append(("CueSet", {"time": at, "name": cue.get("name") or ""}))

        # Кліпи в лінійці: структури знімок не створює, але значення вирівнює.
        for track in state.get("tracks") or []:
            tid = (track or {}).get("id")
            if not tid:
                continue
            ops.extend(self._arrangement_ops({"id": tid},
                                             track.get("arrangement") or []))

        for scene in state.get("scenes") or []:
            if not scene.get("id"):
                continue
            # Темп і метр сцени -- частина документа: сцена, що мовчки перемикає
            # темп в одного і не перемикає в іншого, розводить пару миттєво.
            timing = scene.get("timing")
            if timing:
                payload = {"scene": {"id": scene["id"]}}
                payload.update(timing)
                ops.append(("SceneTimingSet", payload))
            ops.extend(self._meta_ops("scene", {"id": scene["id"]}, scene))

        return ops

    def _arrangement_ops(self, ref, clips):
        """Кліпи в лінійці зі знімка.

        Створення тут немає навмисно: знімок не будує структури, він
        вирівнює значення в тих кліпах, що в партнера вже є.
        """
        ops = []
        for clip in clips:
            uid = (clip or {}).get("id")
            if not uid:
                continue
            clip_ref = {"id": uid}
            for prop, value in (clip.get("props") or {}).items():
                ops.append(("ClipPropSet", {"track": ref, "clip": clip_ref,
                                            "prop": prop, "value": value}))
            markers = clip.get("warp")
            if markers:
                ops.append(("ClipWarpSet", {"track": ref, "clip": clip_ref,
                                            "markers": markers}))
            loop = clip.get("loop")
            if loop:
                payload = {"track": ref, "clip": clip_ref}
                payload.update(loop)
                ops.append(("ClipLoopSet", payload))
            for region, part in self._note_regions_for(
                    {"length": clip.get("length")}, clip.get("notes") or []):
                if not part:
                    continue
                ops.append(("ArrangementClipNotesSet", {
                    "track": ref, "clip": clip_ref,
                    "region": region, "notes": part}))
            for prop in ("name", "color"):
                if clip.get(prop) is None:
                    continue
                ops.append(("ObjectMetaSet", {"object": "clip", "track": ref,
                                              "clip": clip_ref, "prop": prop,
                                              "value": clip[prop]}))
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
        for param in ("volume", "panning", "crossfader", "cue_volume",
                      "crossfade_assign"):
            if param in mixer:
                ops.append(("MixerSet", {"track": ref, "param": param, "value": mixer[param]}))
        for send in mixer.get("sends") or []:
            if send.get("value") is None:
                continue
            payload = {"track": ref, "param": "send",
                       "index": send.get("index"), "value": send.get("value")}
            # uuid Return -- контрольна сума: індекс сенда між машинами не
            # збігається, щойно набір Return-треків розійшовся.
            ret = send.get("return") or {}
            if ret.get("id"):
                payload["return"] = ret
            ops.append(("MixerSet", payload))
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
            # Маркери семплу -- не параметри, у них власний блок
            for prop, value in (entry.get("sample") or {}).items():
                payload = {"track": ref, "device": device, "prop": prop, "value": value}
                if chain_path:
                    payload["chain_path"] = chain_path
                ops.append(("SamplePropSet", payload))
            for prop, value in (entry.get("state") or {}).items():
                payload = {"track": ref, "device": device, "prop": prop, "value": value}
                if chain_path:
                    payload["chain_path"] = chain_path
                ops.append(("DeviceStateSet", payload))
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
            # Властивості й warp -- після створення кліпу й нот: на порожній
            # слот вони не лягли б, а warp вимагає ще й того, щоб кліп був audio.
            for prop, value in (entry.get("props") or {}).items():
                ops.append(("ClipPropSet", {"track": ref, "scene": scene,
                                            "prop": prop, "value": value}))
            markers = entry.get("warp")
            if markers:
                ops.append(("ClipWarpSet", {"track": ref, "scene": scene,
                                            "markers": markers}))
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
        "cues": [{"time": t, "name": n} for t, n in sorted(self._cue_map().items())],
        # Мікшер ланцюгів окремим списком, а не всередині дерева девайсів:
        # ланцюг адресується власним uuid, і в дереві він лише контейнер.
        "chains": [dict({"id": rec["id"]},
                        **self._chain_state(self._chains_reg.obj_of(rec["id"])))
                   for rec in self._chain_records
                   if self._chains_reg.obj_of(rec.get("id")) is not None],
        "song": dict((p, self._song_prop_value(p, self._safe_attr(self._doc, p)))
                     for p in SONG_PROPS
                     if self._song_prop_value(p, self._safe_attr(self._doc, p)) is not None),
            "file_path": file_path,  # daemon виводить із нього теку проєкту
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

    def _script_sha(self):
        """Хеш власного джерела. Версія цього не ловить.

        SCRIPT_VERSION між комітами не змінюється, тож Live, який тримає
        в памʼяті вчорашній файл, виглядає точно як свіжий -- і це вже
        тричі коштувало нам вечора: спершу партнеру з 0.17, потім тут,
        коли SceneTimingSet мовчав, бо в запущеному скрипті його не було.
        Перевірка файлу на диску цього не бачить: вона звіряє диск, а не
        те, що Live прочитав під час старту.

        Шляхів пробуємо кілька, і на це є причина з живого прогону: на
        12.3.5 хеш не доїхав узагалі, а мовчазне порожнє значення нічим не
        відрізняється від старого скрипта, який хеш рахувати ще не вміє.
        Тож тепер невдача не мовчить -- вона каже, що саме не відкрилось.
        """
        tried = []
        for path in self._sha_candidates():
            try:
                with open(path, "rb") as handle:
                    return hashlib.sha256(handle.read()).hexdigest()[:12]
            except Exception as e:
                tried.append("%s (%r)" % (path, e))
        self._warn("хеш скрипта не порахувався, перевірка версій осліпла: %s"
                   % ("; ".join(tried) or "жодного шляху не вийшло скласти",))
        return ""

    @staticmethod
    def _sha_candidates():
        """Де може лежати наше джерело. Порядок -- від найточнішого."""
        out = []
        try:
            raw = __file__
        except Exception:
            return out
        for path in (raw, os.path.abspath(raw)):
            if path.endswith(".pyc") or path.endswith(".pyo"):
                path = path[:-1]
            if path not in out:
                out.append(path)
        # __pycache__/AbletonMP.cpython-311.pyc -> AbletonMP.py поруч із пакетом:
        # відрізання останнього символу тут дає шлях, якого не існує.
        try:
            folder = os.path.dirname(os.path.abspath(raw))
            if os.path.basename(folder) == "__pycache__":
                folder = os.path.dirname(folder)
            direct = os.path.join(folder, "AbletonMP.py")
            if direct not in out:
                out.append(direct)
        except Exception:
            pass
        return out

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
        # Виняток усередині Live -- найдорожчий різновид мовчання: усе
        # виглядає живим, а частина синхронізації просто не працює. Тому
        # коротким рядком він іде й у вікно daemon, де людина дивиться.
        # Один рядок на місце: залп однакових винятків на кожному тіку
        # сам став би другим потоком навантаження.
        if where in self._exc_seen:
            return
        self._exc_seen.add(where)
        try:
            first = traceback.format_exc().strip().splitlines()[-1]
        except Exception:
            first = "?"
        self._warn("виняток у %s: %s (подробиці в bridge.log)" % (where, first))

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
