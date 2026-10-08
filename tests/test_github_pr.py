import json
from pathlib import Path
import subprocess
import sys
from unittest import TestCase, mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_pr_review.common import UnknownDelivery, verify_seal
from ai_pr_review.github_pr import GitHubPRClient, PRReviewPublisher


HEAD = "a" * 40
HEAD2 = "b" * 40
BASE = "c" * 40
REPO = "acme/widgets"


def raw_pr(number=7, head=HEAD):
    return {"number": number, "html_url": f"https://github.com/{REPO}/pull/{number}",
            "title": "Improve widget", "body": "PR body", "draft": False, "state": "open",
            "base": {"ref": "main", "sha": BASE},
            "head": {"sha": head, "repo": {"full_name": "fork/widgets"}}}


def pr(number=7, head=HEAD):
    return GitHubPRClient._normalize_pr(raw_pr(number, head))


def finding(local_id="F1", *, severity="medium", confidence="high", path="src/a.py",
            line=10, commit=HEAD, root="Unchecked widget state"):
    evidence = [{"commit": commit, "path": path, "line_start": line, "line_end": line}]
    if commit != HEAD:
        evidence.append({"commit": HEAD, "path": path, "line_start": 20, "line_end": 20})
    return {"local_id": local_id, "severity": severity, "confidence": confidence,
            "root_cause": root, "expected": "The widget is validated.",
            "observed": "The widget is used before validation.",
             "impact": "Malformed input can fail the request.",
             "remediation_direction": "Validate before use.", "checked_head_sha": HEAD,
             "persists_at_head": "present",
            "evidence": evidence}


def result(model, findings, effort="high", tier="primary"):
    return {"model": model, "effort": effort, "tier": tier, "report": {"findings": findings}}


class ClientTests(TestCase):
    @mock.patch("ai_pr_review.github_pr.subprocess.run")
    def test_open_pr_pagination_and_normalization(self, run):
        first, second = raw_pr(), raw_pr(8)
        second["base"]["ref"] = "release"
        first["body"] = "x" * 40_000
        run.return_value = subprocess.CompletedProcess([], 0, json.dumps([[first], [second]]), "")
        items = GitHubPRClient(timeout=12).list_open_prs(REPO, ["main"])
        self.assertEqual([7], [item["number"] for item in items])
        self.assertEqual(32_000, len(items[0]["body"]))
        self.assertEqual({"base_ref", "base_sha", "body", "draft", "head_repo", "head_sha",
                      "number", "state", "title", "url", "merged", "merge_commit_sha"}, set(items[0]))
        command = run.call_args.args[0]
        self.assertEqual(["gh", "api"], command[:2])
        self.assertIn("--paginate", command)
        self.assertEqual(12, run.call_args.kwargs["timeout"])

    @mock.patch("ai_pr_review.github_pr.subprocess.run")
    def test_create_check_reconciles_existing_identity(self, run):
        check = {"id": 4, "name": "review", "external_id": "run-1", "head_sha": HEAD}
        run.return_value = subprocess.CompletedProcess([], 0, json.dumps([{"check_runs": [check]}]), "")
        self.assertEqual(check, GitHubPRClient().create_check(REPO, HEAD, "review", "run-1"))
        self.assertEqual(1, run.call_count)


class FakeClient:
    def __init__(self):
        self.issue_comments = []
        self.reviews = []
        self.review_comments = []
        self.created_reviews = []
        self.updated = []
        self.timeout_review = False

    def list_issue_comments(self, repo, number):
        return list(self.issue_comments)

    def create_comment(self, repo, number, body):
        item = {"id": 10, "body": body, "html_url": f"https://github.com/{repo}/pull/{number}#issuecomment-10"}
        self.issue_comments.append(item)
        return item

    def update_comment(self, repo, comment_id, body):
        self.updated.append(comment_id)
        item = next(item for item in self.issue_comments if item["id"] == comment_id)
        item["body"] = body
        return item

    def list_reviews(self, repo, number):
        return list(self.reviews)

    def list_review_comments(self, repo, number):
        return list(self.review_comments)

    def create_review(self, repo, number, head, body, comments, *, event):
        item = {"id": len(self.reviews) + 20, "body": body,
                "html_url": f"https://github.com/{repo}/pull/{number}#pullrequestreview-{len(self.reviews) + 20}"}
        self.created_reviews.append({"head": head, "body": body, "comments": comments, "event": event})
        self.reviews.append(item)
        self.review_comments.extend({"body": comment["body"], "commit_id": head,
                                     "pull_request_review_id": item["id"]} for comment in comments)
        if self.timeout_review:
            self.timeout_review = False
            raise UnknownDelivery("unknown")
        return item


class PublisherTests(TestCase):
    def publisher(self, temporary, client=None, **policy):
        return PRReviewPublisher(client or FakeClient(), policy, Path(temporary) / "receipt.json")

    def test_changed_line_filtering_on_both_sides(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as temporary:
            client = FakeClient()
            findings = [finding("R", line=10), finding("L", path="src/deleted.py", line=4, commit=BASE),
                        finding("H", path="src/not-changed.py", line=9)]
            receipt = self.publisher(temporary, client).publish(
                REPO, pr(), HEAD, [result("model/exact", findings)],
                {"src/a.py": {"RIGHT": {10}}, "src/deleted.py": {"LEFT": {4}}})
            comments = client.created_reviews[0]["comments"]
            self.assertEqual([("RIGHT", 10), ("LEFT", 4)], [(item["side"], item["line"]) for item in comments])
            self.assertEqual(2, receipt["inline_count"])
            self.assertEqual(1, receipt["held_count"])
            verify_seal(receipt)

    def test_cap_prioritizes_severity_then_confidence_and_path(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as temporary:
            client = FakeClient()
            findings = [finding("low", severity="low", path="z.py", line=1),
                        finding("high-low-confidence", severity="high", confidence="low", path="b.py", line=1),
                        finding("critical", severity="critical", path="c.py", line=1),
                        finding("high-high-confidence", severity="high", path="a.py", line=1)]
            changed = {item["evidence"][0]["path"]: {"RIGHT": {1}} for item in findings}
            receipt = self.publisher(temporary, client, max_inline_comments=2).publish(
                REPO, pr(), HEAD, [result("model-1", findings)], changed)
            bodies = [item["body"] for item in client.created_reviews[0]["comments"]]
            self.assertIn("### [CRITICAL]", bodies[0])
            self.assertIn("### [HIGH]", bodies[1])
            self.assertIn("HIGH / high confidence", bodies[1])
            self.assertIn("### [CRITICAL]", client.created_reviews[0]["body"])
            self.assertIn("### [HIGH]", client.created_reviews[0]["body"])
            self.assertEqual((2, 2), (receipt["inline_count"], receipt["held_count"]))

    def test_each_model_gets_one_unified_review_with_its_own_inline_comments(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as temporary:
            client = FakeClient()
            same = finding()
            receipt = self.publisher(temporary, client).publish(
                REPO, pr(), HEAD, [result("model-a", [same, dict(same)]), result("model-b", [dict(same)])],
                {"src/a.py": {"RIGHT": {10}}})
            self.assertEqual(2, receipt["inline_count"])
            self.assertEqual(2, len(client.created_reviews))
            self.assertFalse(client.issue_comments)
            for review, model in zip(client.created_reviews, ("model-a", "model-b")):
                self.assertEqual(len(review["comments"]), 1)
                self.assertIn(model, review["body"])
                self.assertIn("Expected:", review["body"])
                self.assertIn(model, review["comments"][0]["body"])
                self.assertEqual(review["event"], "COMMENT")

    def test_same_head_is_idempotent_and_human_comment_is_untouched(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as temporary:
            client = FakeClient()
            human = {"id": 1, "body": "Human note", "html_url": "human"}
            client.issue_comments.append(human)
            publisher = self.publisher(temporary, client)
            args = (REPO, pr(), HEAD, [result("model-a", [finding()])], {"src/a.py": {"RIGHT": {10}}})
            publisher.publish(*args)
            publisher.publish(*args)
            self.assertEqual(1, len(client.created_reviews))
            self.assertEqual("Human note", human["body"])

    def test_new_head_creates_new_unified_review_without_issue_comments(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as temporary:
            client = FakeClient()
            publisher = self.publisher(temporary, client)
            publisher.publish(REPO, pr(), HEAD, [result("m", [finding()])], {"src/a.py": {"RIGHT": {10}}})
            next_finding = finding()
            next_finding["checked_head_sha"] = HEAD2
            next_finding["evidence"][0]["commit"] = HEAD2
            publisher.publish(REPO, pr(head=HEAD2), HEAD2, [result("m", [next_finding])], {"src/a.py": {"RIGHT": {10}}})
            self.assertEqual(2, len(client.created_reviews))
            self.assertEqual([], client.updated)
            self.assertFalse(client.issue_comments)
            self.assertIn(HEAD2, client.created_reviews[-1]["body"])

    def test_timeout_reconciles_marker_without_retry(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as temporary:
            client = FakeClient()
            client.timeout_review = True
            receipt = self.publisher(temporary, client).publish(
                REPO, pr(), HEAD, [result("m", [finding()])], {"src/a.py": {"RIGHT": {10}}})
            self.assertEqual(1, len(client.created_reviews))
            self.assertEqual(20, receipt["reviews"][0]["review_id"])

    def test_no_findings_still_submits_a_review_body(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as temporary:
            client = FakeClient()
            self.publisher(temporary, client).publish(REPO, pr(), HEAD, [result("model-a", [])], {})
            self.assertEqual(len(client.created_reviews), 1)
            self.assertEqual(client.created_reviews[0]["comments"], [])
            self.assertIn("No actionable findings", client.created_reviews[0]["body"])
            self.assertFalse(client.issue_comments)

    def test_withheld_findings_do_not_become_a_clean_review(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as temporary:
            client = FakeClient()
            value = result("model-a", [])
            value["report"]["withheld_count"] = 1
            self.publisher(temporary, client).publish(REPO, pr(), HEAD, [value], {})
            body = client.created_reviews[0]["body"]
            self.assertIn("not a clean-review verdict", body)
            self.assertNotIn("No actionable findings", body)

    def test_blocking_is_disabled_by_default(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as temporary:
            client = FakeClient()
            self.publisher(temporary, client, blocking_severities=["critical"]).publish(
                REPO, pr(), HEAD, [result("m", [finding(severity="critical")])],
                {"src/a.py": {"RIGHT": {10}}})
            self.assertEqual("COMMENT", client.created_reviews[0]["event"])
