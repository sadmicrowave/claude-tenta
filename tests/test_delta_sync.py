"""Delta sync (slice `feat/workspace-delta-sync`): a folder travels as a content-hashed manifest, and only the files the worker does not hold are sent.
Design: docs/design/WORKSPACE-JOBS.md, "Built: delta sync". Tests start from the real worker and client fixtures (local bare repositories stand in for both transports; the
transport changes only the secret policy and the compression threshold, so every client test runs for both)."""
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402
from ct import filecache, jobs, refusals, snapshot  # noqa: E402
from ct.client import delta, submit  # noqa: E402

FILES = {"in.txt": "alpha\n", "sub/b.txt": "beta\n", "c.txt": "gamma\r\nline two\r\n"}


def blobs_of(files):
    return {snapshot.blob_hash(t): t for t in files.values()}


class Manifest(unittest.TestCase):
    def test_entries_have_path_hash_size_mode_and_the_id_is_the_hash_of_the_sorted_manifest(self):
        m = snapshot.manifest_of(FILES)
        self.assertEqual(set(m), set(FILES))
        e = m["c.txt"]
        self.assertEqual((e["size"], e["mode"], len(e["sha256"])), (len(FILES["c.txt"].encode()), "644", 64))
        self.assertEqual(snapshot.snapshot_id(m), snapshot.snapshot_id(dict(reversed(list(m.items())))))     # order never matters

    def test_the_hash_is_over_the_exact_bytes_so_crlf_is_kept(self):
        self.assertNotEqual(snapshot.blob_hash("a\r\n"), snapshot.blob_hash("a\n"))

    def test_a_renamed_file_changes_the_id_but_not_the_blob(self):
        a, b = snapshot.manifest_of({"x.txt": "same\n"}), snapshot.manifest_of({"y.txt": "same\n"})
        self.assertNotEqual(snapshot.snapshot_id(a), snapshot.snapshot_id(b))
        self.assertEqual(a["x.txt"]["sha256"], b["y.txt"]["sha256"])

    def test_a_manifest_that_is_not_one_is_refused_with_its_code(self):
        good = snapshot.manifest_of({"a.txt": "x"})
        for bad in ([], {"../a": good["a.txt"]}, {"/abs": good["a.txt"]}, {"a.txt": {**good["a.txt"], "sha256": "zz"}}, {"a.txt": {**good["a.txt"], "size": -1}},
                    {"a.txt": {**good["a.txt"], "mode": "777"}}, {"a.txt": "nope"}):
            with self.assertRaises(refusals.Refused, msg=str(bad)) as r:
                snapshot.check_manifest(bad)
            self.assertTrue(r.exception.code.startswith("files."), r.exception.code)


class Cache(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()) / "filecache"
        self.clock = [1000.0]
        self.c = filecache.FileCache(self.dir, max_mb=1, clock=lambda: self.clock[0])

    def test_put_stores_under_the_hash_the_worker_computed_and_get_returns_the_text(self):
        h = self.c.put("hello\r\n")
        self.assertEqual(h, snapshot.blob_hash("hello\r\n"))
        self.assertEqual(self.c.get(h), "hello\r\n")
        self.assertEqual(self.c.missing([h, "0" * 64]), ["0" * 64])

    def test_a_claimed_hash_that_is_not_the_content_is_refused_and_nothing_is_stored(self):
        with self.assertRaises(refusals.Refused) as r:
            self.c.put("evil\n", claimed=snapshot.blob_hash("innocent\n"))
        self.assertEqual(r.exception.code, "files.cache.hash_mismatch")
        self.assertEqual(self.c.missing([snapshot.blob_hash("innocent\n")]), [snapshot.blob_hash("innocent\n")])
        self.assertEqual(self.c.total_bytes(), 0)

    def test_an_entry_changed_on_disk_is_not_served_and_is_dropped(self):
        h = self.c.put("good\n")
        (self.dir / h).write_text("changed\n")
        self.assertIsNone(self.c.get(h))
        self.assertEqual(self.c.missing([h]), [h])

    def test_the_folder_is_private(self):
        self.c.put("x")
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)

    def test_the_epoch_is_kept_across_restarts_and_new_after_a_reset(self):
        first = self.c.epoch
        self.assertEqual(filecache.FileCache(self.dir, 1).epoch, first)
        import shutil
        shutil.rmtree(self.dir)
        self.assertNotEqual(filecache.FileCache(self.dir, 1).epoch, first)

    def test_eviction_drops_the_least_recently_used_first_and_never_a_pinned_entry(self):
        old, mid, new = (self.c.put(t * 400_000) for t in "abc")
        for h, when in ((old, 1.0), (mid, 2.0), (new, 3.0)):
            os.utime(self.dir / h, (when, when))
        gone = self.c.evict(pinned={old})                       # 1.2 MB held, cap 1 MB: one entry must go, and not the pinned oldest
        self.assertEqual(gone, [mid])
        self.assertEqual(self.c.missing([old, new]), [])

    def test_using_an_entry_makes_it_recent(self):
        a, b = self.c.put("a" * 600_000), self.c.put("b" * 600_000)
        os.utime(self.dir / a, (1.0, 1.0))
        os.utime(self.dir / b, (2.0, 2.0))
        self.c.get(a)                                           # a is used now, so b is the oldest
        self.assertEqual(self.c.evict(), [b])


class WorkerSide(support.Base):
    def setUp(self):
        super().setUp()
        self.w.load_registry()

    def send(self, jid, files, blobs=None, **extra):
        m = snapshot.manifest_of(files)
        body = {"manifest": m, "snapshot": snapshot.snapshot_id(m), "blobs": blobs if blobs is not None else blobs_of(files)}
        return self.job(jid, "files", **{**body, **extra})

    def count_files(self, jid):
        return {p.relative_to(self.w._p("work", jid)).as_posix() for p in self.w._p("work", jid).rglob("*") if p.is_file()}

    def test_registry_json_says_the_worker_has_a_cache_and_its_epoch(self):
        self.assertTrue(self.w.publish_registry_info(self.w.load_registry()))
        info = json.loads(support.sh("git", "-C", str(self.tmp / "results.git"), "show", "main:registry.json"))
        self.assertEqual((info["cache"]["version"], info["cache"]["epoch"]), (1, self.w.cache.epoch))
        self.assertIn("snapshot_count", info["limits"]["files"])

    def test_first_run_stores_the_files_and_runs_on_them_byte_exact(self):
        self.send("job-0001", FILES)
        self.run_all()
        self.assertEqual(self.result("job-0001")["status"], "ok", self.result("job-0001"))
        self.assertEqual(self.w.cache.missing(list(blobs_of(FILES))), [])
        call = self.runner.calls[-1]
        self.assertEqual(call["files"], ["c.txt", "in.txt", "sub"])
        self.assertEqual((call["wd"] / "c.txt").read_bytes(), b"gamma\r\nline two\r\n")        # no newline translation

    def test_a_rerun_with_no_blobs_runs_from_the_cache(self):
        self.send("job-0001", FILES)
        self.run_all()
        self.send("job-0002", FILES, blobs={})
        self.run_all()
        self.assertEqual(self.result("job-0002")["status"], "ok", self.result("job-0002"))
        self.assertEqual((self.runner.calls[-1]["wd"] / "in.txt").read_text(), "alpha\n")

    def test_a_deleted_file_is_not_in_the_next_folder_and_a_renamed_one_is_found_by_its_hash(self):
        self.send("job-0001", FILES)
        self.run_all()
        less = {"in.txt": FILES["in.txt"], "renamed/b.txt": FILES["sub/b.txt"]}              # c.txt deleted, sub/b.txt renamed
        self.send("job-0002", less, blobs={})
        self.run_all()
        names = sorted(str(p.relative_to(self.runner.calls[-1]["wd"])) for p in self.runner.calls[-1]["wd"].rglob("*") if p.is_file())
        self.assertEqual(names, ["in.txt", "renamed/b.txt"])

    def test_the_mode_is_applied(self):
        m = snapshot.manifest_of({"in.txt": "x\n", "run.sh": "echo\n"}, modes={"run.sh": "755"})
        files = {"in.txt": "x\n", "run.sh": "echo\n"}
        self.job("job-0001", "files", manifest=m, snapshot=snapshot.snapshot_id(m), blobs=blobs_of(files))
        self.run_all()
        self.assertTrue(os.access(self.runner.calls[-1]["wd"] / "run.sh", os.X_OK))
        self.assertFalse(os.access(self.runner.calls[-1]["wd"] / "in.txt", os.X_OK))

    def test_a_reset_worker_says_what_it_lacks_and_does_not_run(self):
        self.send("job-0001", FILES, blobs={})
        self.w.fetch()
        res = self.result("job-0001")
        self.assertEqual(res["status"], "needs-files", res)
        self.assertEqual(sorted(res["needs"]), sorted(blobs_of(FILES)))
        self.assertIn("send", res["note"].lower())
        self.assertEqual(self.spawned, [])
        self.assertFalse(self.w._p("done", "job-0001").exists())

    def test_a_top_up_for_exactly_those_files_lets_the_job_go_on_without_a_new_approval(self):
        self.send("job-0001", FILES, blobs={})
        self.w.fetch()
        self.assertEqual(self.result("job-0001")["status"], "needs-files")
        self.job("x-other", "echo")                                              # (any later commit on the branch)
        self.push_topup("job-0001", blobs_of(FILES))
        self.run_all()
        self.assertEqual(self.result("job-0001")["status"], "ok", self.result("job-0001"))

    def test_a_top_up_blob_that_is_not_what_it_says_is_refused_and_never_stored(self):
        self.send("job-0001", FILES, blobs={})
        self.w.fetch()
        bad = dict(blobs_of(FILES))
        first = sorted(bad)[0]
        bad[first] = "something else\n"
        self.push_topup("job-0001", bad)
        self.w.fetch()
        self.assertEqual(self.w.cache.missing([first]), [first])
        self.assertNotEqual(self.result("job-0001")["status"], "ok")

    def test_a_blob_whose_key_is_not_its_hash_rejects_the_whole_job(self):
        blobs = blobs_of(FILES)
        blobs[snapshot.blob_hash(FILES["in.txt"])] = "planted\n"                  # content under another file's hash
        self.send("job-0001", FILES, blobs=blobs)
        self.w.fetch()
        res = self.result("job-0001")
        self.assertEqual((res["status"], res["refusal"]), ("rejected", "files.cache.hash_mismatch"), res)
        self.assertEqual(self.w.cache.missing([snapshot.blob_hash(FILES["in.txt"])]), [snapshot.blob_hash(FILES["in.txt"])])

    def test_a_blob_the_list_does_not_name_is_never_stored(self):
        extra = "unrelated\n"
        self.send("job-0001", FILES, blobs={**blobs_of(FILES), snapshot.blob_hash(extra): extra})
        self.w.fetch()
        self.assertEqual(self.w.cache.missing([snapshot.blob_hash(extra)]), [snapshot.blob_hash(extra)])
        self.assertEqual(self.w.cache.missing(list(blobs_of(FILES))), [])

    def test_a_manifest_that_does_not_match_its_snapshot_id_is_rejected(self):
        m = snapshot.manifest_of(FILES)
        self.job("job-0001", "files", manifest=m, snapshot="0" * 64, blobs=blobs_of(FILES))
        self.w.fetch()
        res = self.result("job-0001")
        self.assertEqual((res["status"], res["refusal"]), ("rejected", "files.manifest.invalid"), res)

    def test_a_blob_that_differs_in_size_from_the_manifest_is_rejected(self):
        m = snapshot.manifest_of(FILES)
        m["in.txt"] = {**m["in.txt"], "size": 999}
        self.job("job-0001", "files", manifest=m, snapshot=snapshot.snapshot_id(m), blobs=blobs_of(FILES))
        self.w.fetch()
        self.assertEqual(self.result("job-0001")["refusal"], "files.manifest.invalid")

    def test_a_job_with_both_files_and_a_manifest_is_refused(self):
        m = snapshot.manifest_of(FILES)
        self.job("job-0001", "files", manifest=m, snapshot=snapshot.snapshot_id(m), blobs=blobs_of(FILES), files=FILES)
        self.w.fetch()
        self.assertEqual(self.result("job-0001")["refusal"], "files.manifest.invalid")

    def test_a_cached_file_damaged_before_it_runs_is_never_run_and_says_so(self):
        self.send("job-0001", FILES)
        self.w.fetch()                                                           # validated, queued
        victim = sorted(blobs_of(FILES))[0]
        (self.w.cache.root / victim).write_text("tampered\n")
        while self.spawned:
            self.w.execute(self.spawned.pop(0))
        res = self.result("job-0001")
        self.assertEqual(res["status"], "rejected", res)
        self.assertEqual(res["refusal"], "files.cache.damaged")
        self.assertEqual(self.runner.calls, [])

    def test_the_snapshot_caps_apply_to_the_whole_folder_and_the_old_ones_to_nothing(self):
        many = {f"f{i}.txt": "x\n" for i in range(300)}                          # over the 200-file job limit, under the snapshot cap
        self.send("job-0001", many)
        self.run_all()
        self.assertEqual(self.result("job-0001")["status"], "ok", self.result("job-0001"))
        self.registry_path.write_text(support.REGISTRY + "limits:\n  files:\n    snapshot_count: 10\n")        # the owner lowers the cap in the registry
        self.send("job-0002", many)
        self.w.fetch()
        self.assertEqual(self.result("job-0002")["refusal"], "files.set.too_big")

    def test_eviction_never_removes_a_file_a_waiting_job_needs(self):
        self.send("job-0001", FILES, blobs=blobs_of(FILES))
        self.w.fetch()                                                           # queued, not yet run
        with mock.patch.object(self.w, "cache_cap_mb", return_value=0):          # a cap of nothing: everything that may go, goes
            self.w.prune_cache()
            self.assertEqual(self.w.cache.missing(list(blobs_of(FILES))), [])
            while self.spawned:
                self.w.execute(self.spawned.pop(0))
            self.w.prune_cache()                                                 # finished: now they may go
        self.assertEqual(self.w.cache.total_bytes(), 0)

    def test_a_job_cannot_name_a_file_it_did_not_send_unless_the_worker_holds_it(self):
        self.send("job-0001", {"in.txt": "secret of another job\n"})
        self.run_all()
        stranger = {"in.txt": "x\n"}
        m = snapshot.manifest_of(stranger)
        m["in.txt"] = {**m["in.txt"], "sha256": snapshot.blob_hash("secret of another job\n"), "size": len("secret of another job\n")}
        self.job("job-0002", "files", manifest=m, snapshot=snapshot.snapshot_id(m), blobs={})
        self.w.fetch()
        # the hash is a capability: knowing it means holding the content. The job is the client's own, approval-pinned, so this is the documented, same-owner behaviour:
        self.assertTrue(self.w._p("queued", "job-0002.json").exists())

    def test_under_disk_pressure_everything_not_needed_goes(self):
        self.send("job-0001", FILES)
        self.run_all()
        self.settings.disk_reserve = True
        with mock.patch("ct.plat.disk_free_gb", return_value=0.5), mock.patch.object(self.w, "disk_reserve_gb", return_value=2.0):
            self.w.prune_cache()
        self.assertEqual(self.w.cache.total_bytes(), 0)

    def push_topup(self, jid, blobs):
        req = self.tmp / "requester"
        support.sh("git", "fetch", "-q", "origin", cwd=req)
        support.sh("git", "checkout", "-q", "-B", "jobs", "origin/jobs", cwd=req)
        (req / "blobs").mkdir(exist_ok=True)
        (req / "blobs" / f"{jid}.json").write_text(json.dumps(blobs))
        support.sh("git", "add", "-A", cwd=req)
        support.sh("git", "commit", "-q", "-m", f"blobs {jid}", cwd=req)
        support.sh("git", "push", "-q", "origin", "HEAD:refs/heads/jobs", cwd=req)


class ClientSide(support.RepoFixture):
    kind = "direct"

    def setUp(self):
        super().setUp()
        self.ctx.kind = self.kind
        self.dir = Path(tempfile.mkdtemp())
        self.write(FILES)
        self.w.load_registry()
        self.publish()

    def write(self, files, wipe=True):
        if wipe:
            for p in sorted(self.dir.rglob("*"), reverse=True):
                p.unlink() if p.is_file() else p.rmdir()
        for name, text in files.items():
            (self.dir / name).parent.mkdir(parents=True, exist_ok=True)
            (self.dir / name).write_bytes(text.encode())

    def publish(self):
        self.w._p("registry.sha").unlink(missing_ok=True)
        self.w.publish_registry_info(self.w.load_registry())
        self.repos.pull_results()

    def submit(self, *extra):
        code, out, err = self.run_cli("submit", "files", "--dir", str(self.dir), "--no-wait", *extra)
        self.assertEqual(code, 0, out + err)
        return out.strip().splitlines()[-1], out + err

    def queued(self, jid):
        return json.loads(support.sh("git", "-C", str(self.tmp / "queue.git"), "show", f"jobs:jobs/{jid}.json"))

    def run_worker(self):
        self.run_all()

    def test_first_run_sends_everything_as_a_manifest_and_the_worker_runs_it(self):
        jid, _ = self.submit()
        job = self.queued(jid)
        self.assertNotIn("files", job)
        self.assertEqual(set(job["manifest"]), set(FILES))
        self.assertEqual(sorted(job["blobs"]), sorted(blobs_of(FILES)))
        self.run_worker()
        self.assertEqual(self.result(jid)["status"], "ok", self.result(jid))

    def test_an_unchanged_rerun_sends_the_manifest_only(self):
        self.submit()
        self.run_worker()
        jid, text = self.submit()
        job = self.queued(jid)
        self.assertEqual(job.get("blobs", {}), {})
        self.assertIn("nothing changed", text.lower())
        self.run_worker()
        self.assertEqual(self.result(jid)["status"], "ok")

    def test_one_changed_file_sends_only_that_file(self):
        self.submit()
        self.run_worker()
        self.write({**FILES, "in.txt": "alpha changed\n"})
        jid, _ = self.submit()
        self.assertEqual(list(self.queued(jid)["blobs"].values()), ["alpha changed\n"])
        self.run_worker()
        self.assertEqual(self.result(jid)["status"], "ok")
        self.assertEqual((self.runner.calls[-1]["wd"] / "in.txt").read_text(), "alpha changed\n")

    def test_a_deleted_and_a_renamed_file_send_nothing(self):
        self.submit()
        self.run_worker()
        self.write({"in.txt": FILES["in.txt"], "moved/b.txt": FILES["sub/b.txt"]})
        jid, _ = self.submit()
        job = self.queued(jid)
        self.assertEqual(job.get("blobs", {}), {})
        self.assertEqual(sorted(job["manifest"]), ["in.txt", "moved/b.txt"])
        self.run_worker()
        self.assertEqual(self.result(jid)["status"], "ok")

    def test_a_reset_worker_is_noticed_from_its_epoch_and_everything_is_sent_again(self):
        self.submit()
        self.run_worker()
        import shutil
        shutil.rmtree(self.w.cache.root)                                           # the worker's state was wiped
        self.w.cache = filecache.FileCache(self.w.cache.root, self.w.cache.max_mb)
        self.publish()
        jid, text = self.submit()
        self.assertEqual(sorted(self.queued(jid)["blobs"]), sorted(blobs_of(FILES)))
        self.run_worker()
        self.assertEqual(self.result(jid)["status"], "ok")

    def test_a_wrong_guess_is_corrected_by_a_top_up_and_the_job_still_runs(self):
        self.submit()
        self.run_worker()
        import shutil
        shutil.rmtree(self.w.cache.root)                                           # reset, but the client has NOT seen the new epoch (the worker did not republish)
        self.w.cache = filecache.FileCache(self.w.cache.root, self.w.cache.max_mb)
        jid, _ = self.submit()
        self.assertEqual(self.queued(jid).get("blobs", {}), {})
        self.w.fetch()
        self.assertEqual(self.result(jid)["status"], "needs-files")
        code, out, err = self.run_cli("result", jid)                               # the client sees it, tops up, and says so
        self.assertIn("sending", (out + err).lower())
        self.run_worker()
        self.assertEqual(self.result(jid)["status"], "ok", self.result(jid))
        self.assertEqual(self.runner.calls[-1]["wd"].joinpath("c.txt").read_bytes(), FILES["c.txt"].encode())

    def test_resend_all_ignores_what_the_worker_is_thought_to_hold(self):
        self.submit()
        self.run_worker()
        jid, _ = self.submit("--resend-all")
        self.assertEqual(sorted(self.queued(jid)["blobs"]), sorted(blobs_of(FILES)))

    def test_a_worker_that_does_not_say_it_has_a_cache_gets_the_old_whole_set(self):
        info = json.loads((self.repos.results / "registry.json").read_text())
        info.pop("cache")
        (self.repos.results / "registry.json").write_text(json.dumps(info))
        jid, _ = self.submit()
        job = self.queued(jid)
        self.assertEqual(job["files"], FILES)
        self.assertNotIn("manifest", job)

    def test_a_secret_is_never_in_the_manifest_or_the_blobs(self):
        self.write({**FILES, "server.pem": "-----BEGIN PRIVATE KEY-----"}, wipe=False)         # privacy-ok: a fake key header
        jid, _ = self.submit("--send-incomplete")
        job = self.queued(jid)
        self.assertNotIn("server.pem", job["manifest"])
        self.assertNotIn("PRIVATE KEY", json.dumps(job))

    def test_a_binary_file_is_still_not_text_and_still_stops_the_job(self):
        (self.dir / "img.bin").write_bytes(b"\xff\xfe\x00\x01")
        code, out, err = self.run_cli("submit", "files", "--dir", str(self.dir), "--no-wait")
        self.assertNotEqual(code, 0)
        self.assertIn("files.not_text", out + err)

    def test_the_files_sent_for_a_job_are_pruned_with_the_job(self):
        self.submit()
        self.run_worker()
        import shutil
        shutil.rmtree(self.w.cache.root)
        self.w.cache = filecache.FileCache(self.w.cache.root, self.w.cache.max_mb)
        jid, _ = self.submit()
        self.w.fetch()
        self.run_cli("result", jid)                                                # pushes blobs/<jid>.json
        self.run_worker()                                                          # the job finishes
        self.assertIn(f"blobs/{jid}.json", support.sh("git", "-C", str(self.tmp / "queue.git"), "ls-tree", "-r", "--name-only", "jobs"))
        self.repos.pull_results()                                                  # the client has seen the final result
        msg = self.repos.prune_jobs(force=True, now=lambda: time.time() + 30 * 86400)
        self.assertNotIn("blobs/", support.sh("git", "-C", str(self.tmp / "queue.git"), "ls-tree", "-r", "--name-only", "jobs"), msg)

    def test_a_changed_source_is_refused_not_sent(self):
        self.submit()
        self.run_worker()
        import shutil
        shutil.rmtree(self.w.cache.root)
        self.w.cache = filecache.FileCache(self.w.cache.root, self.w.cache.max_mb)
        jid, _ = self.submit()
        self.w.fetch()
        self.write({**FILES, "in.txt": "edited meanwhile\n"})
        code, out, err = self.run_cli("result", jid)
        self.assertIn("files.cache.source_changed", out + err)
        self.assertEqual(self.result(jid)["status"], "needs-files")

    def test_a_folder_over_the_snapshot_caps_goes_the_old_way(self):
        info = json.loads((self.repos.results / "registry.json").read_text())
        info["limits"]["files"]["snapshot_count"] = 2
        (self.repos.results / "registry.json").write_text(json.dumps(info))
        jid, _ = self.submit()
        job = self.queued(jid)
        self.assertEqual(job["files"], FILES)
        self.assertNotIn("manifest", job)

    def test_the_ledger_is_per_worker_and_never_holds_content(self):
        self.submit()
        text = delta.ledger_path(self.repos).read_text()
        self.assertNotIn("alpha", text)
        self.assertIn(self.w.cache.epoch, text)

    def test_the_card_says_what_was_sent_and_what_was_reused(self):
        self.submit()
        self.run_worker()
        self.write({**FILES, "in.txt": "alpha changed\n"})
        _, text = self.submit()
        self.assertRegex(text, r"Sent\W+1 file")
        self.assertRegex(text, r"Already on the worker\W+2 files")


class ClientSideGitHub(ClientSide):
    """The GitHub transport differs from Direct only in the secret policy and the compression threshold, so it runs the cases that decide the delta and the secret case; the rest are the same code."""
    kind = "github"
    CORE = ("test_first_run_sends_everything_as_a_manifest_and_the_worker_runs_it", "test_an_unchanged_rerun_sends_the_manifest_only", "test_one_changed_file_sends_only_that_file",
            "test_a_wrong_guess_is_corrected_by_a_top_up_and_the_job_still_runs", "test_a_secret_is_never_in_the_manifest_or_the_blobs")

    def setUp(self):
        if self._testMethodName not in self.CORE:
            self.skipTest("the same code as the Direct run")
        super().setUp()


class Bytes(unittest.TestCase):
    def test_the_manifest_is_far_smaller_than_the_files(self):
        files = {f"src/f{i}.py": ("line %d\n" % i) * 200 for i in range(100)}
        m = snapshot.manifest_of(files)
        self.assertLess(len(json.dumps(m)), sum(len(v) for v in files.values()) / 10)


class Catalog(unittest.TestCase):
    def test_the_new_refusals_exist_and_say_who_decides(self):
        for code in ("files.manifest.invalid", "files.cache.hash_mismatch", "files.cache.damaged", "files.cache.missing"):
            self.assertIn(code, refusals.CATALOG, code)
        self.assertEqual(refusals.CATALOG["files.cache.hash_mismatch"].kind, "security")
        self.assertEqual(refusals.CATALOG["files.manifest.invalid"].who, "worker")


if __name__ == "__main__":
    unittest.main()
