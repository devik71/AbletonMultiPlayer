# -*- coding: utf-8 -*-
"""Стабільна ідентичність об'єктів Live.

Індекс треку не є адресою: вставка треку зсуває всі наступні, і подія від
партнера поїхала б не в той об'єкт. Реєстр тримає `uuid -> пряме посилання
на LOM-об'єкт`. Посилання переживає зсув індексів, бо Python тримає сам об'єкт,
а не його позицію.

Видалений у Live об'єкт лишається в реєстрі, але звертання до нього кидає
RuntimeError -- це і є tombstone з vision.md §5.6: дії над неіснуючим об'єктом
тихо ігноруються, а не застосовуються в порожнечу.
"""

try:
    from uuid import uuid4

    def new_id():
        return uuid4().hex[:12]
except ImportError:  # на випадок урізаного stdlib у процесі Live
    import random

    def new_id():
        return "%012x" % random.getrandbits(48)


def _same(a, b):
    """Live-проксі коректно порівнюються через ==, але мертвий об'єкт кидає."""
    try:
        return a == b
    except Exception:
        return False


def _alive(obj):
    try:
        obj.name
        return True
    except Exception:
        return False


class Registry(object):
    """Плаский список пар (uuid, об'єкт).

    Лінійний пошук свідомо: на реальному проєкті це десятки об'єктів, а не тисячі,
    і словник тут не збудувати -- Live-проксі не гарантують стабільний хеш.
    """

    def __init__(self, log):
        self._log = log
        self._entries = []

    def clear(self):
        self._entries = []

    def __len__(self):
        return len(self._entries)

    def id_of(self, obj, create=True):
        """uuid об'єкта; за потреби видає новий."""
        for uid, o in self._entries:
            if _same(o, obj):
                return uid
        if not create:
            return None
        uid = new_id()
        self._entries.append((uid, obj))
        return uid

    def obj_of(self, uid):
        """Живий об'єкт за uuid, або None якщо він невідомий чи вже видалений."""
        for u, o in self._entries:
            if u == uid:
                return o if _alive(o) else None
        return None

    def taken_by_other(self, uid, obj):
        """Чи належить цей uuid іншому ЖИВОМУ об'єкту.

        Ctrl+D у Live копіює трек разом із set_data, тож копія приходить із
        ідентифікатором джерела. Довіритись йому означало б віддати ідентичність
        випадковому з двох, а події для копії -- застосовувати до оригіналу.
        Мертвий власник не рахується: він уже tombstone, його id вільний.
        """
        for u, o in self._entries:
            if u != uid:
                continue
            if _same(o, obj):
                return False
            return _alive(o)
        return False

    def bind(self, uid, obj):
        """Прив'язує наперед відомий uuid -- бootstrap від першого гравця.

        Знімає і попередній uuid цього об'єкта, і попереднього власника цього
        uuid, щоб не лишалось двох записів на одну сутність.
        """
        self._entries = [(u, o) for (u, o) in self._entries
                         if u != uid and not _same(o, obj)]
        self._entries.append((uid, obj))

    def known_ids(self):
        return [u for u, _ in self._entries]

    def forget(self, uid):
        self._entries = [(u, o) for (u, o) in self._entries if u != uid]

    def diff(self, objects):
        """Звіряє реєстр із поточним станом Live.

        Повертає (нові, зниклі): нові одразу отримують uuid, зниклі -- ті, чиї
        uuid більше не належать жодному об'єкту зі списку. Зниклі НЕ забуваються
        тут -- це вирішує викликач, бо для tombstone їх іноді треба лишити.
        """
        created = []
        seen = set()
        for i, obj in enumerate(objects):
            uid = self.id_of(obj, create=False)
            if uid is None:
                uid = self.id_of(obj)
                created.append((uid, i, obj))
            seen.add(uid)
        removed = [u for u in self.known_ids() if u not in seen]
        return created, removed
