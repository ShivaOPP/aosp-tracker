import asyncio
import base64
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from aosp_tracker import api_server, gofile, rom_scanner, upload
from aosp_tracker.formatter import make_rom_device_keyboard
from aosp_tracker.monitor import AospMonitor, log_tail
from aosp_tracker.telegram_client import TelegramClient


def make_monitor():
    with patch("aosp_tracker.monitor.load_global_config", return_value={}), patch.object(AospMonitor, "load_state"):
        monitor = AospMonitor()
    monitor.telegram = Mock()
    monitor.server_name = "Worker"
    return monitor


class UploadTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name, "ROM #1.zip")
        self.path.write_bytes(b"test release")
        self.rom = {"main_zip_name": self.path.name, "files_to_upload": ["boot.img", str(self.path)]}

    def check_transfer(self, provider, body, expected):
        with patch("aosp_tracker.upload.subprocess.Popen") as spawn:
            proc = spawn.return_value.__enter__.return_value
            proc.communicate.return_value = body, b""
            proc.returncode = 0
            active = []
            result = upload.upload_release(self.rom, provider, {"pixeldrain_api_key": "test-key"}, active_procs=active)
            self.assertEqual(result["download_page"], expected)
            self.assertEqual(result["uploaded_files"][0]["size_bytes"], len(b"test release"))
            self.assertEqual(active, [])
            args = spawn.call_args.args[0]
            self.assertEqual(args[args.index("--upload-file") + 1], str(self.path))
            self.assertTrue(args[-1].endswith("/ROM%20%231.zip"))
            self.assertNotIn("test-key", str(args))
            return proc.communicate.call_args.args[0], args[-1]

    @patch.dict(os.environ, {}, clear=True)
    def test_pixeldrain(self):
        header, url = self.check_transfer("pixeldrain", b'{"id":"Abc123","success":true}', "https://pixeldrain.com/u/Abc123")
        self.assertEqual(header.strip(), b"Authorization: Basic " + base64.b64encode(b":test-key"))
        self.assertTrue(url.startswith("https://pixeldrain.com/api/file/"))

    def test_fuckingfast_created_file_metadata(self):
        body = b'{"code":201,"data":{"id":"abC123def456","isDirectory":false}}'
        self.check_transfer("fuckingfast", body, "https://fuckingfast.net/abC123def456")
        with self.assertRaises(ValueError):
            upload.share_url("fuckingfast", '{"code":201,"data":{"id":"../unexpected"}}')

    def test_fuckingfast(self):
        header, url = self.check_transfer("fuckingfast", b"https://fuckingfast.net/file#name", "https://fuckingfast.net/file#name")
        self.assertEqual(header, b"")
        self.assertTrue(url.startswith("https://w.fuckingfast.net/"))

    @patch.dict(os.environ, {}, clear=True)
    def test_missing_key_and_missing_package(self):
        with patch("aosp_tracker.upload.subprocess.Popen") as spawn:
            self.assertIn("API_KEY", upload.upload_release(self.rom, "pixeldrain", {})["error"])
            self.path.unlink()
            self.assertFalse(upload.upload_release(self.rom, "fuckingfast", {})["success"])
            spawn.assert_not_called()

    def test_rejects_bad_share_links(self):
        for body in ("<html>error</html>", "https://example.com/test", '{"url":"https://fuckingfast.net@invalid/file"}'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                upload.share_url("fuckingfast", body)

    def test_http_error_and_cancel(self):
        with patch("aosp_tracker.upload.subprocess.Popen") as spawn:
            proc = spawn.return_value.__enter__.return_value
            proc.communicate.return_value = b"", b"HTTP error 413"
            proc.returncode = 22
            self.assertIn("413", upload.upload_release(self.rom, "fuckingfast", {})["error"])
            cancel = threading.Event()
            proc.communicate.side_effect = [upload.subprocess.TimeoutExpired("curl", 1), (b"", b"")]
            result = upload.upload_release(self.rom, "fuckingfast", {}, lambda snap: cancel.set(), cancel, [])
            self.assertTrue(result["cancelled"])
            proc.kill.assert_called_once()

    def test_gofile_folder_reuses_guest_token(self):
        companion = self.path.with_name("boot.img")
        companion.write_bytes(b"boot")
        self.rom["files_to_upload"] = [str(self.path), str(companion)]
        with patch.object(gofile, "upload_single_file", side_effect=[
            {"id": "1", "parentFolder": "folder", "guestToken": "guest", "downloadPage": "https://gofile.io/d/code"},
            {"id": "2"},
        ]) as put:
            result = upload.upload_release(self.rom, "gofile", {})
        self.assertEqual(len(result["uploaded_files"]), 2)
        first, second = [call.kwargs for call in put.call_args_list]
        self.assertEqual(first["server"], "upload")
        self.assertEqual(first["file_path"], str(companion))
        self.assertEqual(second["folder_id"], "folder")
        self.assertEqual(second["token"], "guest")

    def test_missing_subset_never_sends_other_files(self):
        rom = {"files_to_upload": ["/boot-directory/release.zip"]}
        self.assertEqual(upload.select_artifacts(rom, "boot")["files_to_upload"], [])


class WorkerTests(unittest.TestCase):
    def test_large_error_log_uses_bounded_reads_and_streams_to_telegram(self):
        data = b"compile\n" * 80000 + b"final error\n"
        with tempfile.NamedTemporaryFile() as log:
            log.write(data)
            log.flush()
            self.assertEqual(log_tail(log.name, 2), ["compile\n", "final error\n"])
            def receive(req, timeout):
                chunks = list(req.data)
                self.assertLessEqual(max(map(len, chunks)), 256 * 1024)
                self.assertEqual(b"".join(chunks[1:-1]), data)
                self.assertEqual(sum(map(len, chunks)), int(req.get_header("Content-length")))
                return io.BytesIO(b'{"ok": true, "result": {"message_id": 42}}')
            with patch("urllib.request.urlopen", side_effect=receive):
                self.assertEqual(TelegramClient("dummy").send_document("1", log.name), 42)

    def test_empty_catalog_is_cached_and_forced_refresh_works(self):
        with patch.object(rom_scanner, "_LAST_SCAN_TIME", float("-inf")), patch.object(rom_scanner, "_SCAN_CACHE", []), patch.object(rom_scanner, "_scan_roms", return_value=[]) as scan:
            rom_scanner.scan_all_roms().append("caller mutation")
            self.assertEqual(rom_scanner.scan_all_roms(), [])
            self.assertEqual(scan.call_count, 1)
            rom_scanner.scan_all_roms(True)
            self.assertEqual(scan.call_count, 2)

    def test_slow_library_request_does_not_serialize_status(self):
        entered, release, answered = threading.Event(), threading.Event(), threading.Event()
        class MemoryConnection:
            def __init__(self, path):
                self.request = io.BytesIO(f"GET /{path} HTTP/1.0\r\n\r\n".encode())
                self.response = bytearray()
            def settimeout(self, timeout): pass
            def makefile(self, *args): return self.request
            def sendall(self, data): self.response.extend(data)
            def shutdown(self, how): pass
            def close(self): pass
        def slow_catalog():
            entered.set()
            release.wait(4)
            return []
        def status():
            answered.set()
            return {"online": True}
        def server_factory(address, handler):
            # Run the actual HTTP parser and dispatch using in-memory connections.
            server = object.__new__(ThreadingHTTPServer)
            server.RequestHandlerClass = handler
            server.serve_forever = lambda: None
            return server
        server = api_server.StatusServer(get_status_callback=status, get_roms_callback=slow_catalog)
        with patch.object(api_server, "ThreadingHTTPServer", side_effect=server_factory):
            server.start()
        try:
            server.httpd.process_request(MemoryConnection("api/roms"), ("localhost", 1))
            self.assertTrue(entered.wait(1))
            server.httpd.process_request(MemoryConnection("status"), ("localhost", 2))
            self.assertTrue(answered.wait(1))
        finally:
            release.set()

    def test_poll_errors_are_distinct_from_idle_polls(self):
        client = TelegramClient("dummy")
        with patch.object(client, "_post", return_value=None), self.assertRaises(ConnectionError):
            client.get_updates()
        with patch.object(client, "_post", return_value={"ok": True, "result": []}):
            self.assertEqual(client.get_updates(), [])

    def test_stale_upload_id_is_rejected(self):
        monitor = make_monitor()
        monitor.get_local_roms = Mock(return_value=[{"id": "rel_new", "device": "sample"}])
        self.assertFalse(monitor.handle_remote_upload_request("sample", "42", "rel_old")["success"])


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_misses_share_one_scan(self):
        monitor = make_monitor()
        async def scan(force):
            await asyncio.sleep(0.01)
            return []
        monitor.fetch_catalog = AsyncMock(side_effect=scan)
        await asyncio.gather(monitor.get_all_multi_server_roms(), monitor.get_all_multi_server_roms())
        await monitor.get_all_multi_server_roms()
        monitor.fetch_catalog.assert_awaited_once()

    async def test_help_remains_available_during_catalog_scan(self):
        monitor = make_monitor()
        entered, release = asyncio.Event(), asyncio.Event()
        async def scan(force):
            entered.set()
            await release.wait()
            return []
        monitor.fetch_catalog = scan
        def message(cmd):
            return {"text": cmd, "chat": {"id": -1, "type": "group"}, "from": {"id": 1}}
        task = asyncio.create_task(monitor.handle_telegram_message(message("/roms")))
        await asyncio.wait_for(entered.wait(), 1)
        self.assertIn("Opening", monitor.telegram.send_message.call_args.args[1])
        await asyncio.wait_for(monitor.handle_telegram_message(message("/help")), 1)
        self.assertIn("/status", monitor.telegram.send_message.call_args.args[1])
        release.set()
        await task
        monitor.telegram.edit_message.assert_called_once()

    async def test_worker_post_publishes_selected_package_and_updates_card(self):
        monitor = make_monitor()
        monitor.loop = asyncio.get_running_loop()
        monitor.group_chat_id = "-1"
        monitor.telegram.send_message.return_value = 10
        completed = asyncio.Event()
        def edit(**kwargs):
            if "UPLOAD COMPLETE" in kwargs["text"]:
                monitor.loop.call_soon_threadsafe(completed.set)
            return True
        monitor.telegram.edit_message.side_effect = edit
        with tempfile.NamedTemporaryFile(suffix=".zip") as release:
            rom = {"id": "rel_123", "name": "Nightly", "device": "sample",
                   "main_zip_name": os.path.basename(release.name), "files_to_upload": [release.name, "boot.img"]}
            monitor.get_local_roms = Mock(return_value=[rom])
            handler = object.__new__(api_server.StatusRequestHandler)
            handler.expected_secret = "test"
            handler.start_upload_callback = monitor.handle_remote_upload_request
            handler.path = "/api/upload"
            handler.send_json_response = Mock()
            body = json.dumps({"chat_id": "-1", "rom_id": "rel_123", "provider": "fuckingfast", "mode": "pkg"}).encode()
            handler.rfile = io.BytesIO(body)
            handler.headers = {"Content-Length": len(body), "X-AOSP-Secret": "test"}
            result = {"success": True, "download_page": "https://fuckingfast.net/sample",
                      "uploaded_files": [{"name": rom["main_zip_name"], "size_formatted": "0 B"}]}
            with patch("aosp_tracker.monitor.upload_release", return_value=result) as put:
                await asyncio.to_thread(handler.do_POST)
                await asyncio.wait_for(completed.wait(), 1)
                self.assertEqual(handler.send_json_response.call_args.args[1], 200)
                selected, host, config = put.call_args.args
                self.assertEqual(host, "fuckingfast")
                self.assertEqual(selected["files_to_upload"], [release.name])
                self.assertIn("FUCKINGFAST.NET", monitor.telegram.edit_message.call_args.kwargs["text"])

    async def test_worker_receives_selected_provider_and_its_own_id(self):
        monitor = make_monitor()
        monitor.peer_servers = [{"name": "Worker alias", "url": "https://worker.invalid/status"}]
        rom = {"id": "peer_1", "name": "ROM", "worker_id": "rel_123", "server_name": "Worker alias",
               "device": "sample", "package_hosts": ["fuckingfast"]}
        monitor.get_all_multi_server_roms = AsyncMock(return_value=[rom])
        data = make_rom_device_keyboard(rom)["inline_keyboard"][0][0]["callback_data"]
        cb = {"id": "cb", "data": data, "message": {"chat": {"type": "group", "id": -1}},
              "from": {"id": next(iter(monitor.admin_ids))}}
        with patch("aosp_tracker.monitor.read_json", return_value={"success": True}) as submit:
            await monitor.handle_telegram_callback(cb)
            req = submit.call_args.args[0]
            self.assertEqual(json.loads(req.data), {"rom_id": "rel_123", "chat_id": "-1", "device": "sample",
                                                    "provider": "fuckingfast", "mode": "pkg"})
            submit.reset_mock()
            cb["from"]["id"] = 1
            await monitor.handle_telegram_callback(cb)
            submit.assert_not_called()
