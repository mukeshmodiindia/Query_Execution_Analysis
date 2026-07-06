import json
import unittest

from src.parsers import normalize_query, parse_mongodb_logs


class MongoDBParserTests(unittest.TestCase):
    def test_parses_structured_slow_query_attr_command(self):
        log_line = json.dumps(
            {
                "t": {"$date": "2026-06-30T00:55:26.123+00:00"},
                "s": "I",
                "c": "COMMAND",
                "msg": "Slow query",
                "attr": {
                    "type": "command",
                    "ns": "prod.orders",
                    "command": {
                        "find": "orders",
                        "filter": {"status": "PENDING", "customerId": 1001},
                        "$db": "prod",
                    },
                    "planSummary": "IXSCAN { status: 1, customerId: 1 }",
                    "durationMillis": 240,
                },
            }
        )

        events = parse_mongodb_logs(log_line)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].namespace, "prod.orders")
        self.assertEqual(events[0].duration_ms, 240)
        self.assertEqual(events[0].plan_summary, "IXSCAN { status: 1, customerId: 1 }")
        self.assertIn('"find": "orders"', events[0].query)

    def test_prefers_originating_command_for_getmore(self):
        log_line = json.dumps(
            {
                "t": {"$date": "2026-06-30T00:55:27.000+00:00"},
                "msg": "Slow query",
                "attr": {
                    "ns": "prod.orders",
                    "command": {"getMore": 123, "collection": "orders", "$db": "prod"},
                    "originatingCommand": {
                        "aggregate": "orders",
                        "pipeline": [{"$match": {"status": "PENDING"}}],
                        "$db": "prod",
                    },
                    "durationMillis": 860,
                },
            }
        )

        events = parse_mongodb_logs(log_line)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].duration_ms, 860)
        self.assertIn('"aggregate": "orders"', events[0].query)
        self.assertNotIn('"getMore"', events[0].query)

    def test_parses_legacy_text_slow_query_line(self):
        line = (
            '2026-06-30T00:55:26.000+0000 I COMMAND [conn42] command prod.orders '
            'command: find { find: "orders", filter: { status: "PENDING", customerId: 1001 }, '
            'sort: { createdAt: -1 } } planSummary: IXSCAN keysExamined:100 docsExamined:100 '
            'cursorExhausted:1 numYields:0 nreturned:10 reslen:2048 315ms'
        )

        events = parse_mongodb_logs(line)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].namespace, "prod.orders")
        self.assertEqual(events[0].duration_ms, 315)
        self.assertIn('filter: { status: "PENDING"', events[0].query)

    def test_parses_collection_scan_plan_summary(self):
        log_line = json.dumps(
            {
                "t": {"$date": "2026-06-30T00:55:28.000+00:00"},
                "msg": "Slow query",
                "attr": {
                    "ns": "prod.stories",
                    "command": {
                        "find": "stories",
                        "filter": {"diggs": {"$gte": 10}},
                        "$db": "prod",
                    },
                    "planSummary": "COLLSCAN",
                    "durationMillis": 1200,
                },
            }
        )

        events = parse_mongodb_logs(log_line)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].namespace, "prod.stories")
        self.assertEqual(events[0].plan_summary, "COLLSCAN")
        self.assertEqual(events[0].duration_ms, 1200)

    def test_parses_structured_update_command(self):
        log_line = json.dumps(
            {
                "t": {"$date": "2026-06-30T00:55:29.000+00:00"},
                "msg": "Slow query",
                "attr": {
                    "ns": "prod.stories",
                    "command": {
                        "update": "stories",
                        "updates": [
                            {
                                "q": {"tenantId": "acme", "status": "draft"},
                                "u": {"$set": {"status": "published"}},
                            }
                        ],
                        "$db": "prod",
                    },
                    "planSummary": "COLLSCAN",
                    "durationMillis": 980,
                },
            }
        )

        events = parse_mongodb_logs(log_line)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].namespace, "prod.stories")
        self.assertEqual(events[0].plan_summary, "COLLSCAN")
        self.assertIn('"update": "stories"', events[0].query)

    def test_mongodb_normalization_preserves_crud_collection_names_and_sort_direction(self):
        normalized = normalize_query(
            (
                '{"aggregate":"stories","pipeline":[{"$match":{"tenantId":"acme"}},'
                '{"$sort":{"createdAt":-1}}]}'
            ),
            "MongoDB",
        )
        update_normalized = normalize_query(
            '{"update":"stories","updates":[{"q":{"tenantId":"acme"},"u":{"$set":{"status":"published"}}}]}',
            "MongoDB",
        )

        self.assertIn('"aggregate": "stories"', normalized)
        self.assertIn('"createdAt": -1', normalized)
        self.assertIn('"update": "stories"', update_normalized)


if __name__ == "__main__":
    unittest.main()
