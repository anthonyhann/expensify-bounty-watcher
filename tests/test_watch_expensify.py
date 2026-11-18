import unittest

from watch_expensify import describe, detect_alerts, is_wanted


def make_issue(
    number=1,
    labels=("External",),
    title="[$175] Fix a thing",
    body="",
    assignees=(),
    created="2026-11-17T00:00:00Z",
    is_pr=False,
):
    issue = {
        "number": number,
        "title": title,
        "body": body,
        "labels": [{"name": name} for name in labels],
        "assignees": [{"login": login} for login in assignees],
        "comments": 3,
        "created_at": created,
        "html_url": f"https://github.com/Expensify/App/issues/{number}",
    }
    if is_pr:
        issue["pull_request"] = {"url": "https://github.com/Expensify/App/pull/1"}
    return issue


class DescribeTests(unittest.TestCase):
    def test_parses_amount_and_upwork_url(self):
        body = "Job added: https://www.upwork.com/jobs/~022122567490983281705"
        item = describe(make_issue(body=body))
        self.assertEqual(item["amount"], 175.0)
        self.assertEqual(
            item["upwork"],
            "https://www.upwork.com/jobs/~022122567490983281705",
        )
        self.assertTrue(item["claimable"])

    def test_marks_assigned_issue(self):
        item = describe(make_issue(assignees=("reviewer",)))
        self.assertFalse(item["claimable"])
        self.assertEqual(item["assignees"], ["reviewer"])

    def test_missing_amount(self):
        item = describe(make_issue(title="No reward here"))
        self.assertEqual(item["amount"], 0.0)


class FilterTests(unittest.TestCase):
    def test_external_is_wanted(self):
        self.assertTrue(is_wanted(make_issue(labels=("External",))))

    def test_help_wanted_is_wanted(self):
        self.assertTrue(is_wanted(make_issue(labels=("Help Wanted",))))

    def test_internal_is_skipped(self):
        self.assertFalse(is_wanted(make_issue(labels=("Internal", "External"))))

    def test_pull_request_is_skipped(self):
        self.assertFalse(is_wanted(make_issue(is_pr=True)))

    def test_unpaid_issue_is_skipped(self):
        self.assertFalse(is_wanted(make_issue(labels=("Bug",))))


class AlertTests(unittest.TestCase):
    def test_new_external_alerts_once(self):
        stages = {}
        first = detect_alerts([describe(make_issue())], stages)
        self.assertEqual([stage for _, stage in first], ["new"])
        second = detect_alerts([describe(make_issue())], stages)
        self.assertEqual(second, [])

    def test_help_wanted_transition_alerts(self):
        stages = {}
        detect_alerts([describe(make_issue())], stages)
        alerts = detect_alerts(
            [describe(make_issue(labels=("External", "Help Wanted")))],
            stages,
        )
        self.assertEqual([stage for _, stage in alerts], ["open"])
        self.assertEqual(stages["1"], "open")

    def test_new_issue_with_help_wanted_is_open(self):
        stages = {}
        alerts = detect_alerts(
            [describe(make_issue(labels=("Help Wanted",)))],
            stages,
        )
        self.assertEqual([stage for _, stage in alerts], ["open"])


if __name__ == "__main__":
    unittest.main()
