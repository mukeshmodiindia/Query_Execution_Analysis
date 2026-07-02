import unittest

from src.mongo_indexes import (
    duplicate_index_rows,
    parse_mongo_indexes,
    redundant_index_rows,
    review_suggested_index,
    suggested_index_from_example,
)


class MongoIndexTests(unittest.TestCase):
    def test_parses_get_indexes_json(self):
        indexes = parse_mongo_indexes(
            """
            [
              {"name":"_id_","key":{"_id":1}},
              {"name":"status_1_customerId_1","key":{"status":1,"customerId":1}}
            ]
            """,
            default_collection="orders",
        )

        self.assertEqual(len(indexes), 2)
        self.assertEqual(indexes[1].collection, "orders")
        self.assertEqual(indexes[1].keys, (("status", "1"), ("customerId", "1")))

    def test_marks_exact_suggestion_as_already_existing(self):
        indexes = parse_mongo_indexes(
            '[{"name":"status_1_customerId_1","key":{"status":1,"customerId":1}}]',
            default_collection="orders",
        )
        suggested = suggested_index_from_example(
            'db.getCollection("orders").createIndex({"status": 1, "customerId": 1})'
        )

        review = review_suggested_index(suggested, indexes)

        self.assertEqual(review.status, "Already exists")
        self.assertEqual(review.matched_indexes, ("status_1_customerId_1",))

    def test_marks_suggestion_as_covered_by_longer_compound_index(self):
        indexes = parse_mongo_indexes(
            '[{"name":"status_1_customerId_1_createdAt_-1","key":{"status":1,"customerId":1,"createdAt":-1}}]',
            default_collection="orders",
        )
        suggested = suggested_index_from_example(
            'db.getCollection("orders").createIndex({"status": 1, "customerId": 1})'
        )

        review = review_suggested_index(suggested, indexes)

        self.assertEqual(review.status, "Covered by existing index")

    def test_finds_duplicate_and_redundant_existing_indexes(self):
        indexes = parse_mongo_indexes(
            """
            [
              {"name":"status_1","key":{"status":1}},
              {"name":"status_1_duplicate","key":{"status":1}},
              {"name":"status_1_customerId_1","key":{"status":1,"customerId":1}}
            ]
            """,
            default_collection="orders",
        )

        self.assertEqual(len(duplicate_index_rows(indexes)), 1)
        self.assertEqual(len(redundant_index_rows(indexes)), 2)


if __name__ == "__main__":
    unittest.main()
