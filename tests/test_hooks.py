import unittest

from hooks import add_action, add_filter, apply_filters, clear_hooks, do_action


class HookTests(unittest.TestCase):
    def tearDown(self):
        clear_hooks()

    def test_filters_run_by_priority_and_return_value(self):
        calls = []

        def late(value, context):
            calls.append(("late", context["source"]))
            return value + " late"

        def early(value, context):
            calls.append(("early", context["source"]))
            return value + " early"

        add_filter("example", late, priority=20)
        add_filter("example", early, priority=5)

        result = apply_filters("example", "start", {"source": "test"})

        self.assertEqual(result, "start early late")
        self.assertEqual(calls, [("early", "test"), ("late", "test")])

    def test_actions_run_by_priority(self):
        calls = []

        add_action("example", lambda payload, context: calls.append(("second", payload)), priority=20)
        add_action("example", lambda payload, context: calls.append(("first", context["source"])), priority=5)

        do_action("example", {"ok": True}, {"source": "test"})

        self.assertEqual(calls, [("first", "test"), ("second", {"ok": True})])


if __name__ == "__main__":
    unittest.main()
