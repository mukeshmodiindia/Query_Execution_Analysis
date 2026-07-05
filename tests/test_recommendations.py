import unittest

from src.recommendations import recommendations_for_query


class MongoRecommendationTests(unittest.TestCase):
    def test_aggregate_query_suggests_match_index(self):
        recommendations = recommendations_for_query(
            "MongoDB",
            '{"aggregate":"stories","pipeline":[{"$match":{"diggs":{"$gte":10}}},{"$sort":{"createdAt":-1}}]}',
            "prod.stories",
        )

        examples = [recommendation.example or "" for recommendation in recommendations]
        self.assertTrue(any('"diggs": 1' in example for example in examples))
        self.assertTrue(any('"createdAt": 1' in example for example in examples))


if __name__ == "__main__":
    unittest.main()
