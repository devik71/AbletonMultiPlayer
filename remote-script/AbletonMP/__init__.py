# -*- coding: utf-8 -*-
"""Точка входу, яку викликає Live при виборі скрипта в Preferences > Link/Tempo/MIDI."""

from .AbletonMP import AbletonMP


def create_instance(c_instance):
    return AbletonMP(c_instance)
