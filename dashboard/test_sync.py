"""Tests for sync.py. Run: python3 -m unittest discover dashboard"""
import json
import pathlib
import sys
import unittest
from email.message import EmailMessage

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import sync  # noqa: E402

COURSE = "UE Example Course 123.456"


def report(run_id, started, finished, changes=(), action_events=(), actions=(), baseline=False):
    return {
        "summary": {"runId": run_id, "mode": "quick", "status": "OK", "baseline": baseline, "messages": [],
                    "startedAt": started, "finishedAt": finished, "counts": {"courses": 1, "errors": 0}},
        "changes": list(changes), "actionEvents": list(action_events), "actions": list(actions),
    }


def checkmark_churn(cid="c1"):
    return {"id": cid, "kind": "item_status_changed", "sev": "info", "course": COURSE, "mod": "checkmark",
            "cmId": 1, "item": "Cross 1", "title": "Status changed",
            "detail": {"before": {"checked": 0, "examples": 6, "rows": "a"},
                       "after": {"checked": 0, "examples": 6, "rows": "b"}}}


class NoiseTests(unittest.TestCase):
    def test_checkmark_hash_churn_is_noise(self):
        self.assertEqual(sync.noise_reason(checkmark_churn()), "checkmark hash churn")

    def test_real_checkmark_progress_is_kept(self):
        c = checkmark_churn()
        c["detail"]["after"]["checked"] = 6
        self.assertIsNone(sync.noise_reason(c))

    def test_empty_grade_placeholder_is_noise(self):
        c = {"kind": "grade_changed", "sev": "important", "detail": {"before": None, "after": "- Grade analysis"}}
        self.assertEqual(sync.noise_reason(c), "empty grade placeholder")

    def test_real_grade_is_kept(self):
        c = {"kind": "grade_changed", "sev": "important", "detail": {"after": "87.50 Grade analysis"}}
        self.assertIsNone(sync.noise_reason(c))

    def test_meeting_link_rolling_forward_is_noise(self):
        c = {"kind": "item_date_changed", "sev": "important", "mod": "zoom", "detail": {"role": "open"}}
        self.assertEqual(sync.noise_reason(c), "recurring meeting link")


class BuildTests(unittest.TestCase):
    def test_merges_duplicates_and_drops_reverts(self):
        new_assign = {"id": "a1", "kind": "item_added", "sev": "important", "course": COURSE, "mod": "assign",
                      "cmId": 42, "item": "Sheet 1", "title": 'New assign: "Sheet 1"',
                      "detail": {"dates": {"due": 1800000000}}}
        new_action = {"stage": "new", "key": "assign:42", "course": COURSE, "cmId": 42, "mod": "assign",
                      "item": "Sheet 1", "kind": "submit", "due": 1800000000}
        grade = {"id": "g1", "kind": "grade_changed", "sev": "important", "course": COURSE, "item": "Quiz",
                 "title": "Grade: Quiz → 8.00 Grade analysis (range 0–10)",
                 "detail": {"after": "8.00 Grade analysis", "pct": "80.00 %"}}
        graded_status = {"id": "s1", "kind": "item_status_changed", "sev": "important", "course": COURSE,
                         "mod": "assign", "cmId": 7, "item": "Quiz", "title": "Graded",
                         "detail": {"before": {"grade": None}, "after": {"grade": "8.00 / 10.00"}}}
        date_a = {"id": "d1", "kind": "item_date_changed", "sev": "important", "course": COURSE, "mod": "assign",
                  "cmId": 9, "item": "Old", "detail": {"role": "due", "before": 100, "after": None}}
        date_b = {"id": "d2", "kind": "item_date_changed", "sev": "important", "course": COURSE, "mod": "assign",
                  "cmId": 9, "item": "Old", "detail": {"role": "due", "before": None, "after": 100}}
        reports = [
            report("2026-01-01T1000Z", "2026-01-01T10:00:00Z", "2026-01-01T10:01:00Z",
                   changes=[new_assign, grade, graded_status, date_a, checkmark_churn()], action_events=[new_action]),
            report("2026-01-02T1000Z", "2026-01-02T10:00:00Z", "2026-01-02T10:01:00Z", changes=[date_b],
                   actions=[{"key": "assign:42", "course": COURSE, "item": "Sheet 1", "due": 1800000000,
                             "kind": "submit", "flags": {}}]),
        ]
        data = sync.build(reports, [], days=7)
        types = sorted(e["type"] for e in data["feed"])
        self.assertEqual(types, ["grade", "new-task"])
        grade_ev = next(e for e in data["feed"] if e["type"] == "grade")
        self.assertEqual(grade_ev["pct"], 80.0)
        self.assertEqual(data["filtered"]["raw"], 6)
        self.assertEqual(data["filtered"]["reasons"]["reverted change"], 2)
        self.assertEqual(data["actions"][0]["item"], "Sheet 1")
        self.assertEqual(data["courses"][COURSE]["code"], "123.456")

    def test_baseline_action_events_are_not_news(self):
        ev = {"stage": "new", "key": "quiz:1", "course": COURSE, "cmId": 1, "mod": "quiz", "item": "Q", "kind": "take-quiz"}
        data = sync.build([report("2026-01-01T1000Z", "2026-01-01T10:00:00Z", "2026-01-01T10:01:00Z",
                                  action_events=[ev], baseline=True)], [])
        self.assertEqual(data["feed"], [])


class CourseAndInboxTests(unittest.TestCase):
    def test_parse_course(self):
        c = sync.parse_course("VL / UE Mathematics for AI III 357.260")
        self.assertEqual((c["type"], c["name"], c["code"]), ("VL / UE", "Mathematics for AI III", "357.260"))
        c = sync.parse_course("KS Volunteering - „Learning“ 510.026")
        self.assertEqual(c["name"], "Volunteering")

    def test_classify_inbox(self):
        due = sync.classify_inbox("JKU Moodle: Due on Thursday, 26 November 2026, 1:00 PM: Übung06")
        self.assertEqual((due["kind"], due["title"]), ("due", "Übung06"))
        self.assertEqual(sync.classify_inbox("JKU Moodle: New sign in to your JKU Moodle account")["kind"], "signin")
        sub = sync.classify_inbox("JKU Moodle: Assignment submission confirmation - Upload: Quiz 3")
        self.assertEqual((sub["kind"], sub["title"]), ("submitted", "Upload: Quiz 3"))
        content = sync.classify_inbox("JKU Moodle: 344.053, KV Special Topics, A Person, 2026W new content")
        self.assertEqual(content["title"], "KV Special Topics 344.053 — new content")


class MailTests(unittest.TestCase):
    def test_extract_report_from_robot_mail(self):
        payload = report("2026-01-01T1000Z", "2026-01-01T10:00:00Z", "2026-01-01T10:01:00Z")
        msg = EmailMessage()
        msg["Subject"] = "[moodle-robot] 2026-01-01T1000Z quick OK · 0 changes · 0 action events"
        msg.set_content("MOODLE ROBOT REPORT v1")
        msg.add_attachment(json.dumps(payload).encode(), maintype="application", subtype="json",
                           filename="changes-2026-01-01T1000Z.json")
        name, data = sync.extract_report(msg.as_bytes())
        self.assertEqual(name, "changes-2026-01-01T1000Z.json")
        self.assertEqual(data["summary"]["runId"], "2026-01-01T1000Z")

    def test_header_parsing(self):
        raw = b"Subject: =?utf-8?q?JKU_Moodle:_=C3=9Cbung?=\r\nDate: Thu, 26 Nov 2026 09:48:05 +0100\r\nFrom: noreply@jku.at\r\n\r\n"
        row = sync.parse_header_block(raw, "7")
        self.assertEqual(row["subject"], "JKU Moodle: Übung")
        self.assertEqual(row["date"], "2026-11-26T08:48:05+00:00")

    def test_js_output_cannot_close_script_tag(self):
        js = sync.to_js({"x": "</script><script>alert(1)</script><!--"})
        self.assertNotIn("</script", js)
        self.assertNotIn("<!--", js)


if __name__ == "__main__":
    unittest.main()
