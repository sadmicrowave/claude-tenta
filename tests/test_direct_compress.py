"""Direct sends a set over 1 GB compressed, on by default (task direct-compress). The worker unpacks it under a size cap and a free-disk check (security finding F5);
every way a client could fail to unpack names the command that sends the job again with --no-compress. Tests start from the real worker and client fixtures."""
import base64
import io
import json
import os
import sys
import tempfile
import unittest
import zlib
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402
from armada import jobs, refusals  # noqa: E402
from armada.client import cli, submit, view  # noqa: E402

FILES = {"a.txt": "alpha\n" * 50, "sub/b.txt": "beta\n" * 50}
UNPACK = ("files.unpack.too_big", "files.unpack.no_room", "files.unpack.unreadable")
COMPRESS = ("files.compress.worker_cannot", "files.compress.over_worker_cap")


def packed(files, size=None, data=None):
    z = submit.compress_files(files)
    if size is not None:
        z["size"] = size
    if data is not None:
        z["data"] = data
    return z


class Format(unittest.TestCase):
    def test_a_set_goes_through_compression_and_comes_back_exactly(self):
        z = submit.compress_files(FILES)
        self.assertLess(len(z["data"]), sum(len(v) for v in FILES.values()))
        job = jobs.unpack_files({"class": "files", "files_z": z}, cap_mb=10, free_gb=100, reserve_gb=2)
        self.assertEqual(job["files"], FILES)
        self.assertNotIn("files_z", job)

    def test_a_job_with_plain_files_is_left_as_it_is(self):
        job = {"class": "files", "files": FILES}
        self.assertIs(jobs.unpack_files(job, cap_mb=10, free_gb=100, reserve_gb=2), job)

    def test_a_zip_bomb_is_stopped_whatever_size_it_claims(self):
        bomb = base64.b64encode(zlib.compress(json.dumps({"x": "0" * 5_000_000}).encode(), 9)).decode()
        self.assertLess(len(bomb), 10_000)                                                      # a few KB that unpack to 5 MB
        with self.assertRaises(refusals.Refused) as r:
            jobs.unpack_files({"files_z": {"codec": submit.CODEC, "size": 10, "data": bomb}}, cap_mb=1, free_gb=100, reserve_gb=2)     # claims 10 bytes
        self.assertEqual(r.exception.code, "files.unpack.too_big")

    def test_a_claim_over_the_cap_is_refused_before_anything_is_unpacked(self):
        with self.assertRaises(refusals.Refused) as r:
            jobs.unpack_files({"files_z": packed(FILES, size=5_000_000_000)}, cap_mb=1000, free_gb=100, reserve_gb=2)
        self.assertEqual(r.exception.code, "files.unpack.too_big")

    def test_not_enough_free_disk_is_refused_first(self):
        with self.assertRaises(refusals.Refused) as r:
            jobs.unpack_files({"files_z": packed(FILES, size=900_000_000)}, cap_mb=1000, free_gb=2.5, reserve_gb=2)
        self.assertEqual(r.exception.code, "files.unpack.no_room")

    def test_damaged_or_odd_data_is_refused_with_its_own_code(self):
        good = submit.compress_files(FILES)
        for z in ({**good, "data": "!!!not base64"}, {**good, "data": good["data"][:-12]}, {**good, "codec": "rot13"}, {"codec": submit.CODEC}, "text",
                  {**good, "data": base64.b64encode(zlib.compress(b"[1, 2]")).decode()}):
            with self.assertRaises(refusals.Refused, msg=str(z)[:40]) as r:
                jobs.unpack_files({"files_z": z}, cap_mb=10, free_gb=100, reserve_gb=2)
            self.assertEqual(r.exception.code, "files.unpack.unreadable", str(z)[:40])

    def test_a_job_with_both_forms_is_refused(self):
        with self.assertRaises(refusals.Refused) as r:
            jobs.unpack_files({"files": FILES, "files_z": packed(FILES)}, cap_mb=10, free_gb=100, reserve_gb=2)
        self.assertEqual(r.exception.code, "files.unpack.unreadable")


class Streaming(unittest.TestCase):
    """compress_files writes the JSON a piece at a time (task 4c): same bytes as before, and no whole-set copies."""

    def test_same_bytes_as_json_dumps(self):
        for files in ({}, FILES, {"é/ü.txt": 'q"uote\\ \n\u2028 日本', "e.txt": ""}):
            z = submit.compress_files(files)
            raw = zlib.decompress(base64.b64decode(z["data"]))
            self.assertEqual(raw, json.dumps(files).encode())
            self.assertEqual(z["size"], len(raw))

    def test_peak_extra_memory_is_far_below_the_set(self):
        import tracemalloc
        files = {"f%d.txt" % i: ("line %d of the file\n" % i) * 20_000 for i in range(10)}      # about 4 MB of text that compresses well, so the output is small and only the input copies show
        total = sum(len(v) for v in files.values())
        tracemalloc.start()
        z = submit.compress_files(files)
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        self.assertGreater(len(z["data"]), 0)
        self.assertLess(peak, total * 0.5)         # the old code peaked above 2x the set (json text, then encoded bytes)


class Catalog(unittest.TestCase):
    def test_every_new_refusal_is_in_the_catalog_and_names_the_command_without_compression(self):
        for code in UNPACK + COMPRESS:
            e = refusals.CATALOG[code]
            self.assertIn("--no-compress", " ".join(e.steps), code)
            self.assertIn("release", e.depends.lower(), code)                                  # which worker release can do it is noted
        self.assertEqual(refusals.CATALOG["files.compress.worker_cannot"].who, "client")
        self.assertEqual(refusals.CATALOG["files.unpack.too_big"].who, "worker")


class WorkerSide(support.Base):
    def setUp(self):
        super().setUp()
        self.registry_path.write_text(support.REGISTRY)
        self.w.load_registry()
        p = mock.patch("armada.plat.disk_free_gb", return_value=5000)          # the free-disk check must not depend on how full the machine running the tests is
        p.start()
        self.addCleanup(p.stop)

    def validate(self, files_z):
        jid = "job-0001"
        raw = json.dumps({"id": jid, "class": "files", "params": {}, "created": support.util.iso(), "files_z": files_z}).encode()
        return self.w._validate(jid, raw, self.w.load_registry(), {})

    def test_the_worker_unpacks_a_compressed_job_into_its_files(self):
        *_, files, _ = self.validate(submit.compress_files(FILES))
        self.assertEqual(files, FILES)

    def test_a_bomb_becomes_a_rejected_result_with_the_code(self):
        bomb = base64.b64encode(zlib.compress(json.dumps({"x": "0" * 30_000_000}).encode(), 9)).decode()
        with mock.patch.object(self.w, "unpacked_cap_mb", return_value=1):
            self.job("job-0002", "files", files_z={"codec": submit.CODEC, "size": 5, "data": bomb})
            self.w.fetch()
        res = self.result("job-0002")
        self.assertEqual((res["status"], res["refusal"]), ("rejected", "files.unpack.too_big"), res)

    def test_the_cap_follows_free_disk_and_memory_and_the_job_ceiling(self):
        self.w.snapshot = lambda: {"disk_free_gb": 8, "mem_available_mb": 100000}
        self.assertEqual(self.w.unpacked_cap_mb(), 2000)                                       # a quarter of the free disk
        self.w.snapshot = lambda: {"disk_free_gb": 5000, "mem_available_mb": 100000}
        self.assertEqual(self.w.unpacked_cap_mb(), 10 * self.w.job_ceiling_mb())              # never more than 10x what a job file may be
        self.w.snapshot = lambda: {"disk_free_gb": 5000, "mem_available_mb": 4000}
        self.assertEqual(self.w.unpacked_cap_mb(), 2000)                                       # half of the free memory

    def test_the_worker_says_in_registry_json_that_it_can_unpack(self):
        self.assertTrue(self.w.publish_registry_info(self.w.load_registry()))
        info = json.loads(support.sh("git", "-C", str(self.tmp / "results.git"), "show", "main:registry.json"))
        self.assertEqual(info["compress"]["codec"], submit.CODEC)
        self.assertEqual(info["compress"]["max_unpacked_mb"], self.w.unpacked_cap_mb())


class ClientSide(support.RepoFixture):
    def setUp(self):
        super().setUp()
        self.ctx.kind = "direct"
        self.big = mock.patch.object(submit, "BIG_SET_BYTES", 100)                              # "over 1 GB" at test size
        self.big.start()
        self.addCleanup(self.big.stop)
        self.dir = Path(tempfile.mkdtemp())
        for name, text in FILES.items():
            (self.dir / name).parent.mkdir(exist_ok=True)
            (self.dir / name).write_text(text)
        (self.dir / "server.pem").write_text("-----BEGIN PRIVATE KEY-----")      # privacy-ok: a fake key header, so the secret scan has something to hold back

    def tell(self, **compress):
        """The worker's published registry as the client reads it, with `compress` (None: an older worker that does not say)."""
        info = json.loads(self.repos.registry_info() and json.dumps(self.repos.registry_info()) or "{}")
        info.update({"limits": {"files": {"max_total_kb": 1_000_000, "max_file_kb": 1_000_000, "max_count": 100000}}})
        if compress:
            info["compress"] = compress
        else:
            info.pop("compress", None)
        (self.repos.results / "registry.json").write_text(json.dumps(info))

    def submit(self, *extra):
        return self.run_cli("submit", "files", "--dir", str(self.dir), "--no-wait", *extra)

    def test_a_big_set_on_direct_is_compressed_by_default_and_the_worker_unpacks_it(self):
        self.tell(codec=submit.CODEC, max_unpacked_mb=1000)
        code, out, err = self.submit()
        self.assertEqual(code, 0, out + err)
        job = json.loads(support.sh("git", "-C", str(self.tmp / "queue.git"), "show", f"jobs:jobs/{out.strip()}.json"))
        self.assertNotIn("files", job)
        self.assertEqual(jobs.unpack_files(job, 1000, 100, 2)["files"], FILES)
        self.assertNotIn("server.pem", jobs.unpack_files(job, 1000, 100, 2)["files"])         # the secret check ran before compressing: the key never went in

    def test_the_card_says_compressed_with_sizes_before_and_after_and_has_no_old_notice(self):
        self.tell(codec=submit.CODEC, max_unpacked_mb=1000)
        code, out, err = self.submit()
        text = out + err
        self.assertIn("Compressed", text)
        self.assertRegex(text, r"\d+ bytes?.*(to|->|→).*\d+ bytes?")
        self.assertNotIn("very large set", text)
        self.assertNotIn("Direct has no size limit", text)

    def test_a_set_under_the_threshold_is_not_compressed(self):
        self.tell(codec=submit.CODEC, max_unpacked_mb=1000)
        with mock.patch.object(submit, "BIG_SET_BYTES", 10_000_000):
            code, out, err = self.submit()
        job = json.loads(support.sh("git", "-C", str(self.tmp / "queue.git"), "show", f"jobs:jobs/{out.strip()}.json"))
        self.assertEqual(job["files"], FILES)
        self.assertNotIn("Compressed", out + err)

    def test_no_compress_sends_plain_files_and_the_card_says_so(self):
        self.tell(codec=submit.CODEC, max_unpacked_mb=1000)
        code, out, err = self.submit("--no-compress")
        job = json.loads(support.sh("git", "-C", str(self.tmp / "queue.git"), "show", f"jobs:jobs/{out.strip()}.json"))
        self.assertEqual(job["files"], FILES)
        self.assertIn("Not compressed", out + err)

    def test_a_worker_that_does_not_say_it_can_unpack_is_refused_before_sending_with_the_command(self):
        self.tell()                                                                             # an older worker release: no "compress" in its registry.json
        code, out, err = self.submit()
        text = out + err
        self.assertNotEqual(code, 0)
        self.assertIn("files.compress.worker_cannot", text)
        self.assertIn("--no-compress", text)
        self.assertIn(f"arm-run submit files --dir {self.dir} --no-wait --no-compress", text.replace("'", ""))      # the exact command, as typed, plus the flag

    def test_a_set_that_would_unpack_to_more_than_the_worker_allows_is_refused_here_first(self):
        self.tell(codec=submit.CODEC, max_unpacked_mb=0.0001)
        code, out, err = self.submit()
        self.assertNotEqual(code, 0)
        self.assertIn("files.compress.over_worker_cap", out + err)
        self.assertIn("--include", out + err)

    def test_github_is_unchanged(self):
        self.ctx.kind = "github"
        self.tell()
        code, out, err = self.submit()
        self.assertNotIn("ompress", out + err)

    def test_a_worker_refusal_to_unpack_shows_the_command_that_sends_it_again_uncompressed(self):
        res = {"id": "files-abc123", "status": "rejected", "refusal": "files.unpack.too_big", "note": "too big", "output": ""}
        cmd = "arm-run submit files --dir . --no-compress"
        self.assertEqual(view.result_card(res, rerun=cmd)["run"], cmd)
        head, _, tail = view.format_result(res, rerun=cmd)
        self.assertIn(cmd, "\n".join(tail))
        self.assertEqual(cli.rerun_for(self.ctx, {**res, "id": "nope"}), None)
        self.ctx.commands.remember("files-abc123", ["submit", "files", "--dir", ".", "--no-wait"])
        self.assertEqual(cli.rerun_for(self.ctx, res), "arm-run submit files --dir . --no-wait --no-compress")


if __name__ == "__main__":
    unittest.main()
