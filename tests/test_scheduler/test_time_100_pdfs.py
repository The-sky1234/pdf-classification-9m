from scripts.time_100_pdfs import page_plan, pages_to_read


def test_page_plan_is_one_hundred_scans():
    plan = page_plan(100)
    assert len(plan) == 100
    assert plan.count(1) == 60 and plan.count(2) == 30 and plan.count(5) == 10


def test_key_ocr_stops_at_two_pages_or_four_hundred_chars():
    assert pages_to_read([120, 80, 400]) == 2
    assert pages_to_read([500, 100]) == 1
    assert pages_to_read([10]) == 1
