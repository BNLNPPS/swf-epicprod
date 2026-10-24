"""Exercise the host trust boundary using real Git repos, without a deploy/API call."""
import fcntl
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


POLLER = Path(__file__).resolve().parents[1] / "scripts/autodeploy-poll.sh"
CANONICAL = "https://github.com/BNLNPPS/swf-epicprod.git"


class PollTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.origin = self.root / "origin.git"
        self.author = self.root / "author"
        self.repo = self.root / "checkout"
        self.shared = self.root / "shared"
        self.shared.mkdir()
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
        self.run_git("init", "--bare", str(self.origin))
        self.run_git("init", "-b", "main", str(self.author))
        self.run_git("config", "user.name", "test", cwd=self.author)
        self.run_git("config", "user.email", "test@example.invalid", cwd=self.author)
        self.run_git("remote", "add", "origin", str(self.origin), cwd=self.author)
        (self.author / "scripts").mkdir()
        (self.author / "scripts/autodeploy.sh").write_text(
            '#!/bin/bash\nset -eu\nflock -n 9\necho deployed >> "$EPICPROD_POLL_SHARED/executed"\n'
            'if [[ -e "$EPICPROD_POLL_SHARED/fail-deploy" ]]; then exit 17; fi\n'
            'git rev-parse HEAD > "$EPICPROD_POLL_SHARED/epicprod-deployed-commit"\n'
        )
        self.first = self.commit("initial")
        self.run_git("push", "-u", "origin", "main", cwd=self.author)
        self.run_git("clone", "-b", "main", str(self.origin), str(self.repo))
        self.run_git("remote", "set-url", "origin", CANONICAL, cwd=self.repo)
        # Only the fixture's transport is redirected; raw origin stays canonical.
        self.run_git("config", f"url.{self.origin.as_uri()}.insteadOf", CANONICAL, cwd=self.repo)
        self.run_git("config", "user.name", "test", cwd=self.repo)
        self.run_git("config", "user.email", "test@example.invalid", cwd=self.repo)
        self.state = self.shared / "epicprod-deployed-commit"
        self.state.write_text(self.first + "\n")
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text('#!/bin/bash\necho "$*" >> "$EPICPROD_POLL_SHARED/status-posts"\n'
                      '[[ ! -e "$EPICPROD_POLL_SHARED/fail-post" ]]\n')
        gh.chmod(0o755)
        self.env.update(EPICPROD_POLL_REPO=str(self.repo), EPICPROD_POLL_SHARED=str(self.shared),
                        PATH=str(bin_dir) + ":" + os.environ["PATH"])

    def run_git(self, *args, cwd=None):
        result = subprocess.run(["git", *args], cwd=cwd, env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def commit(self, message):
        self.run_git("add", ".", cwd=self.author)
        self.run_git("commit", "--allow-empty", "-m", message, cwd=self.author)
        return self.run_git("rev-parse", "HEAD", cwd=self.author)

    def advance(self):
        target = self.commit("merged main change")
        self.run_git("push", "origin", "main", cwd=self.author)
        return target

    def poll(self, succeeds=True):
        result = subprocess.run(["bash", str(POLLER)], env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(result.returncode == 0, succeeds, result.stdout)
        return result.stdout

    def posts(self):
        path = self.shared / "status-posts"
        return path.read_text() if path.exists() else ""

    def assert_not_executed(self):
        self.assertFalse((self.shared / "executed").exists())

    def test_merged_main_deploys_once_and_posts_status(self):
        target = self.advance()
        self.poll()
        self.assertEqual(self.state.read_text().strip(), target)
        self.poll()
        self.assertEqual((self.shared / "executed").read_text(), "deployed\n")
        self.assertEqual(self.posts().count("state=pending"), 1)
        self.assertEqual(self.posts().count("state=success"), 1)

    def test_pr_branch_never_executes(self):
        self.advance()
        self.run_git("switch", "-c", "untrusted-pr", cwd=self.repo)
        self.assertIn("not on main", self.poll(False))
        self.assert_not_executed()

    def test_modified_deploy_script_never_executes(self):
        self.advance()
        (self.repo / "scripts/autodeploy.sh").write_text('echo unsafe > /dev/null\n')
        self.assertIn("uncommitted", self.poll(False))
        self.assert_not_executed()

    def test_unpushed_main_commit_never_executes(self):
        self.advance()
        self.run_git("commit", "--allow-empty", "-m", "local only", cwd=self.repo)
        self.assertIn("outside canonical main", self.poll(False))
        self.assert_not_executed()

    def test_alternate_origin_never_executes(self):
        self.advance()
        self.run_git("remote", "set-url", "origin", str(self.origin), cwd=self.repo)
        self.assertIn("canonical", self.poll(False))
        self.assert_not_executed()

    def test_already_deployed_does_not_touch_local_work(self):
        self.run_git("switch", "-c", "work-in-progress", cwd=self.repo)
        (self.repo / "scripts/autodeploy.sh").write_text("dirty\n")
        self.poll()
        self.assert_not_executed()
        self.assertEqual((self.repo / "scripts/autodeploy.sh").read_text(), "dirty\n")

    def test_failed_deploy_stays_unrecorded_and_does_not_spam_statuses(self):
        self.advance()
        (self.shared / "fail-deploy").touch()
        self.poll(False)
        self.poll(False)
        self.assertEqual(self.state.read_text().strip(), self.first)
        self.assertEqual(self.posts().count("state=pending"), 1)
        self.assertEqual(self.posts().count("state=failure"), 1)

    def test_main_rewrite_is_refused(self):
        self.run_git("checkout", "--orphan", "replacement", cwd=self.author)
        self.commit("rewritten history")
        self.run_git("push", "--force", "origin", "HEAD:main", cwd=self.author)
        self.poll(False)
        self.assert_not_executed()

    def test_status_api_failure_is_retried_without_redeploy(self):
        (self.shared / "fail-post").touch()
        self.poll(False)
        self.assert_not_executed()
        (self.shared / "fail-post").unlink()
        self.poll()
        self.assert_not_executed()
        self.assertIn("state=success", self.posts())

    def test_manual_deploy_lock_prevents_checkout_changes(self):
        self.advance()
        with (self.shared / "epicprod-autodeploy.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.poll()
            self.assertEqual(self.run_git("rev-parse", "HEAD", cwd=self.repo), self.first)
            self.assert_not_executed()


if __name__ == "__main__":
    unittest.main()
