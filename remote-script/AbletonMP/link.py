# -*- coding: utf-8 -*-
"""UDP-транспорт bridge <-> daemon.

Живе всередині процесу Live, тому все тут неблокуюче і нічого не кидає назовні:
виняток у цьому шарі здатен покласти сам Live.
"""

import json
import socket

HOST = "127.0.0.1"
PORT_DAEMON = 19845  # daemon слухає
PORT_BRIDGE = 19846  # bridge слухає

# Стеля датаграми, придатна на ОБОХ системах, а не теоретичний максимум UDP.
#
# Windows пускає до 65507, а macOS має net.inet.udp.maxdgram = 9216 і просто
# відмовляє в sendto. Виміряно на живій парі: bridge на macOS не міг віддати
# жодного чанка знімка (30 КБ) -- черга ретраїла той самий пакет десять разів
# на секунду, вічно, а `state`, `diff` і віддача знімка партнеру з Mac не
# працювали взагалі. На двох Windows це було б невидимо назавжди.
MAX_DATAGRAM = 8192


class UdpLink(object):
    def __init__(self, log, recv_port=PORT_BRIDGE, send_port=PORT_DAEMON, host=HOST):
        self._log = log
        self._peer = (host, send_port)
        self._sock = None
        self._dropped = 0
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setblocking(False)
            sock.bind((host, recv_port))
            self._sock = sock
        except Exception as e:
            self._log("udp bind failed on %s:%d -- %r" % (host, recv_port, e))

    @property
    def alive(self):
        return self._sock is not None

    def send(self, obj):
        if self._sock is None:
            return False
        try:
            data = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        except Exception as e:
            self._log("send: encode failed %r" % (e,))
            return False
        if len(data) > MAX_DATAGRAM:
            self._log("send: datagram too large (%d bytes), dropped" % len(data))
            return False
        try:
            self._sock.sendto(data, self._peer)
            return True
        except Exception:
            # daemon ще не піднявся або впав -- це нормальний стан, не спамимо в лог
            self._dropped += 1
            if self._dropped % 100 == 1:
                self._log("send: %d datagrams dropped (daemon offline?)" % self._dropped)
            return False

    def poll(self, max_msgs=64):
        """Забирає все, що накопичилось у сокеті. Повертає список dict."""
        out = []
        if self._sock is None:
            return out
        for _ in range(max_msgs):
            try:
                data, _addr = self._sock.recvfrom(MAX_DATAGRAM)
            except Exception:
                break  # порожньо (EWOULDBLOCK) або сокет помер
            if not data:
                continue
            try:
                msg = json.loads(data.decode("utf-8"))
            except Exception as e:
                self._log("poll: bad json %r" % (e,))
                continue
            if isinstance(msg, dict):
                out.append(msg)
        return out

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
