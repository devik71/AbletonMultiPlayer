# -*- coding: utf-8 -*-
"""Планувальник: розкладка на блоки, строк на спроби, зупинка черги.

Це єдиний Python у наборі перевірок, і він тут не випадково: chat.py
виконується всередині Live, тобто перевірити його через JS-дзеркало
неможливо в принципі. А логіка в ньому саме та, що ламається тихо --
формат плану, порядок спроб і момент, коли чергу треба спинити.

Мережі тут немає жодної: усе, що ходило б у OpenAI, підмінене.
"""

import io
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, 'remote-script', 'AbletonMP'))

import chat  # noqa: E402


def quiet(*_args, **_kwargs):
    pass


class NormalizeTest(unittest.TestCase):
    def test_old_plan_is_one_block(self):
        # Формат без stages мусить лишитись робочим: інакше кожен, хто
        # вставляє JSON руками, раптово отримує план, що нічого не робить.
        plan = chat._normalize_plan({"reply": "ok", "actions": [{"op": "set_tempo"}]})
        self.assertEqual(len(plan["stages"]), 1)
        self.assertEqual(plan["stages"][0]["actions"], [{"op": "set_tempo"}])
        self.assertEqual(plan["actions"], [{"op": "set_tempo"}])

    def test_actions_stay_flat_for_old_consumers(self):
        # M4L-панель і /api/exec читають plan["actions"] і про блоки не знають.
        plan = chat._normalize_plan({"stages": [
            {"title": "а", "actions": [{"op": "one"}]},
            {"title": "б", "actions": [{"op": "two"}, {"op": "three"}]},
        ]})
        self.assertEqual([a["op"] for a in plan["actions"]], ["one", "two", "three"])
        self.assertEqual([st["title"] for st in plan["stages"]], ["а", "б"])

    def test_empty_block_does_not_stall_the_queue(self):
        plan = chat._normalize_plan({"stages": [
            {"title": "порожній", "actions": []},
            {"title": "робочий", "actions": [{"op": "one"}]},
        ]})
        self.assertEqual([st["title"] for st in plan["stages"]], ["робочий"])

    def test_too_many_blocks_rejected(self):
        raw = {"stages": [{"actions": [{"op": "x"}]} for _ in range(chat.MAX_STAGES + 1)]}
        self.assertRaises(ValueError, chat._normalize_plan, raw)

    def test_malformed_block_rejected(self):
        self.assertRaises(ValueError, chat._normalize_plan, {"stages": [{"actions": "ні"}]})
        self.assertRaises(ValueError, chat._normalize_plan, {"stages": [{"actions": ["ні"]}]})

    def test_no_actions_no_stages(self):
        plan = chat._normalize_plan({"reply": "нічого робити"})
        self.assertEqual(plan["stages"], [])
        self.assertEqual(plan["actions"], [])


class DeadlineTest(unittest.TestCase):
    def planner(self, timeout, fail_with):
        p = chat.OpenAIPlanner(quiet)
        p.api_key = "test"
        p.timeout = timeout
        p._refresh_api_key = lambda: None
        p.calls = []

        def post(path, payload):
            p.calls.append((path, p._attempt_timeout()))
            raise fail_with()

        p._post = post
        return p

    def test_three_failures_share_one_budget(self):
        # Раніше кожна спроба мала власні 45 секунд, тобто на один запит
        # виходило 135 секунд тиші. Тепер строк спільний, і третя спроба
        # просто не починається, якщо часу не лишилось.
        p = self.planner(4, lambda: RuntimeError("формат"))
        started = time.time()
        self.assertRaises(RuntimeError, p.plan, "зроби щось", {})
        self.assertLess(time.time() - started, 4 + 1)
        # Кожна наступна спроба дістає МЕНШЕ часу, ніж попередня
        budgets = [t for _path, t in p.calls]
        self.assertTrue(all(b > 0 for b in budgets), budgets)
        self.assertLessEqual(budgets[-1], budgets[0])

    def test_config_error_stops_the_chain(self):
        # 401 -- це не збій формату. Наступна спроба піде тим самим ключем
        # у ту саму модель і впаде так само, лише зʼївши решту строку.
        p = self.planner(30, lambda: chat._ConfigError("HTTP 401 invalid key"))
        self.assertRaises(RuntimeError, p.plan, "зроби щось", {})
        self.assertEqual(len(p.calls), 1, "після помилки налаштування спроб більше немає")

    def test_format_error_does_retry(self):
        p = self.planner(30, lambda: RuntimeError("не той JSON"))
        self.assertRaises(RuntimeError, p.plan, "зроби щось", {})
        self.assertEqual(len(p.calls), 3, "збій формату мусить дійти до запасних шляхів")

    def test_deadline_released_after_plan(self):
        p = self.planner(4, lambda: RuntimeError("формат"))
        self.assertRaises(RuntimeError, p.plan, "зроби щось", {})
        self.assertIsNone(p._deadline)
        self.assertEqual(p._attempt_timeout(), 4)


class Stub(object):
    """Рівно те, чого потребує run_stages: черга запитів у Live."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.seen = []

    def live_request(self, command, payload):
        self.seen.append((command, payload))
        outcome = self.outcomes.pop(0) if self.outcomes else {"ok": True}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


run_stages = chat.AIChatServer.run_stages


class StagesTest(unittest.TestCase):
    def plan(self, *titles, **kwargs):
        stages = [{"title": t, "needs_confirmation": False,
                   "actions": [{"op": "noop", "n": i}]}
                  for i, t in enumerate(titles)]
        plan = {"reply": "", "needs_confirmation": kwargs.get("confirm", False),
                "actions": [], "stages": stages}
        for i, flag in enumerate(kwargs.get("confirm_at", [])):
            if flag:
                stages[i]["needs_confirmation"] = True
        return plan

    def test_blocks_run_in_order(self):
        stub = Stub([{"ok": True}, {"ok": True}])
        progress, last = run_stages(stub, self.plan("а", "б"), "зроби")
        self.assertEqual([p["status"] for p in progress], ["ok", "ok"])
        self.assertEqual([p["title"] for p in progress], ["а", "б"])
        self.assertEqual(len(stub.seen), 2)
        self.assertEqual(last, {"ok": True})

    def test_queue_stops_on_first_failure(self):
        # Блоки залежні за побудовою: виконати третій поверх невдалого
        # другого означає отримати сет, якого ніхто не просив.
        stub = Stub([{"ok": True}, {"ok": False}, {"ok": True}])
        progress, _last = run_stages(stub, self.plan("а", "б", "в"), "зроби")
        self.assertEqual([p["status"] for p in progress], ["ok", "failed"])
        self.assertEqual(len(stub.seen), 2, "третій блок не мав виконуватись")

    def test_exception_is_a_failure_not_a_crash(self):
        stub = Stub([RuntimeError("Live мовчить")])
        progress, last = run_stages(stub, self.plan("а", "б"), "зроби")
        self.assertEqual(progress[0]["status"], "failed")
        self.assertIn("Live мовчить", progress[0]["error"])
        self.assertIsNone(last)
        self.assertEqual(len(stub.seen), 1)

    def test_confirmation_on_a_block_stops_before_it(self):
        stub = Stub([{"ok": True}])
        plan = self.plan("а", "б", "в", confirm_at=[False, True, False])
        progress, _last = run_stages(stub, plan, "зроби")
        self.assertEqual([p["status"] for p in progress], ["ok", "confirm"])
        self.assertEqual(len(stub.seen), 1)

    def test_plan_wide_confirmation_executes_nothing(self):
        stub = Stub([{"ok": True}])
        progress, last = run_stages(stub, self.plan("а", "б", confirm=True), "зроби")
        self.assertEqual([p["status"] for p in progress], ["confirm", "confirm"])
        self.assertEqual(stub.seen, [])
        self.assertIsNone(last)

    def test_execute_false_only_plans(self):
        stub = Stub([{"ok": True}])
        progress, last = run_stages(stub, self.plan("а"), "зроби", execute=False)
        self.assertEqual(progress, [])
        self.assertIsNone(last)
        self.assertEqual(stub.seen, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class ContractTest(unittest.TestCase):
    def test_both_contracts_survive_normalization(self):
        # Приклад, який ми показуємо моделі, мусить проходити наш власний
        # розбір. Інакше ми вчимо її формату, який самі ж відхиляємо.
        one = chat._normalize_plan(dict(chat.PLAN_CONTRACT))
        self.assertEqual(len(one["stages"]), 1)
        self.assertEqual(len(one["actions"]), len(chat.PLAN_CONTRACT["actions"]))

        staged = chat._normalize_plan(dict(chat.PLAN_CONTRACT_STAGED))
        self.assertEqual(len(staged["stages"]), 3)
        self.assertEqual(len(staged["actions"]), 6)
        self.assertLessEqual(len(staged["stages"]), chat.MAX_STAGES)

    def test_system_prompt_mentions_blocks(self):
        # Схема дозволяє stages, але модель про них дізнається лише з промпту.
        self.assertIn("stages", chat.SYSTEM_PROMPT)
        self.assertIn("stages", chat.PLAN_SCHEMA["properties"])


class BridgeShapeTest(unittest.TestCase):
    """Форма самого AbletonMP.py -- те, що Python перевіряє лише під час виклику.

    Метод у класі без self і без @staticmethod парситься, імпортується
    й мовчить -- аж поки його не викличуть у живому Live. Саме так
    _device_tree_sig упав на першому ж прогоні парою, посеред
    _prime_devices: дзеркало девайсів лишилось непобудованим, і про це
    сказав тільки traceback у bridge.log.
    """

    def bridge_tree(self):
        import ast
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            os.pardir, 'remote-script', 'AbletonMP', 'AbletonMP.py')
        with io.open(path, encoding='utf-8-sig') as fh:
            return ast.parse(fh.read())

    def test_every_method_takes_self_or_is_declared_static(self):
        import ast
        bad = []
        for node in ast.walk(self.bridge_tree()):
            if not isinstance(node, ast.ClassDef):
                continue
            for item in node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                decorators = set()
                for d in item.decorator_list:
                    name = getattr(d, 'id', None) or getattr(d, 'attr', None)
                    if name:
                        decorators.add(name)
                if decorators & {'staticmethod', 'classmethod', 'property'}:
                    continue
                args = item.args.posonlyargs + item.args.args if hasattr(item.args, 'posonlyargs') \
                    else item.args.args
                first = args[0].arg if args else None
                if first not in ('self', 'cls'):
                    bad.append('%s.%s(%s)' % (node.name, item.name, first or ''))
        self.assertEqual(bad, [],
                         'метод у класі без self і без @staticmethod: %s' % ', '.join(bad))


    def test_hello_is_built_in_one_place(self):
        # Копій було дві: на старті скрипта і на hello_request. Друга
        # відстала й не несла хеша -- а доїжджає саме вона, бо daemon майже
        # завжди стартує пізніше за Live. Розбіжність була тиха: перевірка
        # версій вважала свіжий скрипт старим і скаржилась щоразу.
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            os.pardir, 'remote-script', 'AbletonMP', 'AbletonMP.py')
        with io.open(path, encoding='utf-8-sig') as fh:
            src = fh.read()
        self.assertEqual(src.count(chr(34) + 'm' + chr(34) + ': ' + chr(34) + 'hello' + chr(34)), 1,
                         'hello збирається більш ніж в одному місці')
