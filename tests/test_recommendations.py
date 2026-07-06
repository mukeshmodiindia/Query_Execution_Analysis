import unittest

from src.recommendations import (
    mongo_collection_for_query,
    mongo_operation_for_query,
    recommendations_for_query,
)


class MongoRecommendationTests(unittest.TestCase):
    def test_aggregate_query_suggests_match_index(self):
        recommendations = recommendations_for_query(
            "MongoDB",
            '{"aggregate":"stories","pipeline":[{"$match":{"diggs":{"$gte":10}}},{"$sort":{"createdAt":-1}}]}',
            "prod.stories",
        )

        examples = [recommendation.example or "" for recommendation in recommendations]
        self.assertTrue(any('"diggs": 1' in example for example in examples))
        self.assertTrue(any('"createdAt": -1' in example for example in examples))

    def test_find_query_uses_esr_index_order(self):
        recommendations = recommendations_for_query(
            "MongoDB",
            (
                '{"find":"stories","filter":{"tenantId":"acme","diggs":{"$gte":10}},'
                '"sort":{"createdAt":-1}}'
            ),
            "prod.stories",
        )

        examples = [recommendation.example or "" for recommendation in recommendations]
        self.assertTrue(
            any('{"tenantId": 1, "createdAt": -1, "diggs": 1}' in example for example in examples)
        )
        self.assertTrue(any("ESR" in recommendation.title for recommendation in recommendations))

    def test_identifies_crud_write_query_shape(self):
        query = (
            '{"update":"stories","updates":[{"q":{"tenantId":"acme","status":"draft"},'
            '"u":{"$set":{"status":"published"}}}]}'
        )

        recommendations = recommendations_for_query("MongoDB", query, "prod.stories")

        self.assertEqual(mongo_operation_for_query(query, "prod.stories"), "Update")
        self.assertEqual(mongo_collection_for_query(query, "prod.stories"), "stories")
        self.assertTrue(any('"tenantId": 1' in (recommendation.example or "") for recommendation in recommendations))
        self.assertTrue(any('"status": 1' in (recommendation.example or "") for recommendation in recommendations))

    def test_warns_when_aggregate_match_is_after_sort(self):
        recommendations = recommendations_for_query(
            "MongoDB",
            (
                '{"aggregate":"stories","pipeline":[{"$sort":{"createdAt":-1}},'
                '{"$match":{"tenantId":"acme"}}]}'
            ),
            "prod.stories",
        )

        self.assertEqual(
            mongo_operation_for_query(
                '{"aggregate":"stories","pipeline":[{"$sort":{"createdAt":-1}},{"$match":{"tenantId":"acme"}}]}',
                "prod.stories",
            ),
            "Aggregate",
        )
        self.assertTrue(
            any("Move `$match` before blocking aggregation stages" in recommendation.title for recommendation in recommendations)
        )


if __name__ == "__main__":
    unittest.main()
