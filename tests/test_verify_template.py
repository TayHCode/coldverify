import unittest
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[1] / 'ci/templates/verify-coldbox.yml'
FORK = "github.event_name == 'pull_request' && github.event.pull_request.head.repo.full_name != github.repository"


class VerifyTemplate(unittest.TestCase):
    def test_fork_pull_requests_fail_verify_on_a_hosted_runner_before_checkout(self):
        text = TEMPLATE.read_text()
        self.assertEqual(text.count('runs-on:'), 1)
        self.assertNotIn('\n    if:', text)
        self.assertIn("    runs-on: ${{ " + FORK + " && 'ubuntu-latest' || fromJSON('[\"self-hosted\", \"coldbox\"]') }}\n", text)
        refusal = text.index('        if: ' + FORK + '\n')
        self.assertLess(refusal, text.index('actions/checkout'))
        self.assertIn('exit 1', text[refusal:text.index('actions/checkout')])

    def test_template_uses_no_trigger_that_exposes_the_runner_to_forks(self):
        text = TEMPLATE.read_text()
        for trigger in ('pull_request_target', 'workflow_run', 'issue_comment', 'pull_request_review'):
            self.assertNotIn(trigger, text)


if __name__ == '__main__':
    unittest.main()
