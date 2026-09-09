from evaluation.run_evaluation import percentile95, score_case


def test_score_case_counts_catalog_quantity_and_warning_accuracy():
    expected = {
        "id": "eval-1",
        "case_type": "standard",
        "expected_warning_count": 0,
        "should_be_confirmable": True,
        "expected_items": [{
            "product_id": 1,
            "quantity": 5.0,
            "unit": "斤",
            "note": "",
            "matched": True,
        }],
    }
    actual = {
        "items": [{
            "product_id": 1,
            "quantity": 5.0,
            "unit": "斤",
            "note": "",
            "matched": True,
        }],
        "warnings": [],
    }

    score = score_case(expected, actual)
    assert score["product_correct"] == 1
    assert score["quantity_unit_correct"] == 1
    assert score["catalog_matched"] == 1
    assert score["exact_case"] is True


def test_p95_uses_nearest_rank():
    assert percentile95(list(range(1, 101))) == 95
