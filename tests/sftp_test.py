"""Real loopback SFTP/FTP and HTTP Range tests: python tests/sftp_test.py."""
import logging
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import paramiko

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.TemporaryDirectory(prefix="filmax-sftp-")
# resolve()：Windows 的暫存目錄可能是 8.3 短名（MARKCH~1），Files.local 的
# is_relative_to 比的是展開後的長名，兩邊不一致會把所有路徑都當成越界。
BASE = Path(TMP.name).resolve()
os.environ.update({
    "FILMAX_ENV_FILE": str(BASE / "absent.env"),
    "FILMAX_DATA_DIR": str(BASE / "data"),
    "FTP_PROTOCOL": "sftp", "FTP_HOST": "127.0.0.1",
    "FTP_USER": "test", "FTP_PASSWORD": "test-password",
    "FTP_TIMEOUT": "3", "MSSQL_HOST": "", "USER_STORE": "sqlite",
    "MEDIA_STORE": "sqlite", "LIBRARY_LOCAL_ROOTS": "",
    "LIBRARY_ROOTS": "/|auto", "AUTO_SCAN_ON_START": "false",
})
sys.path.insert(0, str(ROOT))
from app import ftpclient, ftpprobe
from app.config import Settings, settings

logging.getLogger("paramiko").setLevel(logging.CRITICAL)
MEDIA = BASE / "media"
MEDIA.mkdir()
PAYLOAD = bytes(range(256)) * 4096
(MEDIA / "中文影片.mp4").write_bytes(PAYLOAD)
(MEDIA / "影集").mkdir()
(MEDIA / "影集" / "第一集.mkv").write_bytes(b"episode")


class Auth(paramiko.ServerInterface):
    def check_auth_publickey(self, username, key):
        return paramiko.AUTH_SUCCESSFUL if username == "test" and key == USER_KEY else paramiko.AUTH_FAILED

    def check_auth_password(self, username, password):
        return (paramiko.AUTH_SUCCESSFUL if (username, password) == ("test", "test-password")
                else paramiko.AUTH_FAILED)

    def get_allowed_auths(self, username):
        return "password,publickey"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class Files(paramiko.SFTPServerInterface):
    def local(self, path):
        local = (MEDIA / path.lstrip("/")).resolve()
        if not local.is_relative_to(MEDIA):
            raise PermissionError(path)
        return local

    def list_folder(self, path):
        try:
            result = []
            for child in self.local(path).iterdir():
                attr = paramiko.SFTPAttributes.from_stat(child.stat())
                attr.filename = child.name
                result.append(attr)
            return result
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(self.local(path).stat())
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    lstat = stat

    def open(self, path, flags, attr):
        try:
            handle = paramiko.SFTPHandle(flags)
            handle.readfile = self.local(path).open("rb")
            return handle
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)


class SftpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = paramiko.RSAKey.generate(2048)
        cls.listener = socket.socket()
        cls.listener.bind(("127.0.0.1", 0))
        cls.listener.listen()
        cls.listener.settimeout(0.2)
        settings.ftp_port = cls.listener.getsockname()[1]
        cls.transports = []
        cls.stop = threading.Event()
        known = BASE / "known_hosts"
        known.write_text(f"[127.0.0.1]:{settings.ftp_port} {cls.key.get_name()} {cls.key.get_base64()}\n")
        settings.sftp_known_hosts = str(known)

        def serve():
            while not cls.stop.is_set():
                try:
                    sock, _ = cls.listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                transport = paramiko.Transport(sock)
                cls.transports.append(transport)
                transport.add_server_key(cls.key)
                transport.set_subsystem_handler("sftp", paramiko.SFTPServer, Files)
                try:
                    transport.start_server(server=Auth())
                except (paramiko.SSHException, EOFError):
                    transport.close()

        cls.thread = threading.Thread(target=serve, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.stop.set()
        cls.listener.close()
        cls.thread.join(3)
        for transport in cls.transports:
            transport.close()

    def test_config_defaults_and_validation(self):
        with patch.dict(os.environ, {"FTP_PORT": "", "FTP_PROTOCOL": "sftp"}):
            self.assertEqual(Settings().ftp_port, 22)
        with patch.dict(os.environ, {"FTP_PORT": "", "FTP_PROTOCOL": "ftp"}):
            self.assertEqual(Settings().ftp_port, 21)
        with patch.dict(os.environ, {"FTP_PROTOCOL": "smtp"}):
            with self.assertRaises(ValueError):
                Settings()

    def test_listing_walk_and_size(self):
        entries = {e.name: e for e in ftpclient.list_dir("/")}
        self.assertTrue(entries["影集"].is_dir)
        self.assertEqual(entries["中文影片.mp4"].size, len(PAYLOAD))
        self.assertEqual(ftpclient.file_size("/中文影片.mp4"), len(PAYLOAD))
        self.assertEqual({p for p, _, _ in ftpclient.walk("/")}, {"/", "/影集"})

    def test_seek_chunk_limit_and_close(self):
        stream = ftpclient.FtpReadStream("/中文影片.mp4", 123)
        self.assertEqual(stream.read(17), PAYLOAD[123:140])
        self.assertEqual(b"".join(stream.iter_chunks(chunk_size=91, limit=400)), PAYLOAD[140:540])
        self.assertIsNone(stream._sftp_file)
        self.assertIsNone(stream._sftp_context)
        stream.close()
        eof = ftpclient.FtpReadStream("/中文影片.mp4", len(PAYLOAD))
        self.assertEqual(b"".join(eof.iter_chunks()), b"")
        interrupted = ftpclient.FtpReadStream("/中文影片.mp4")
        chunks = interrupted.iter_chunks(chunk_size=16)
        self.assertEqual(next(chunks), PAYLOAD[:16])
        chunks.close()
        self.assertIsNone(interrupted._sftp_context)

    def test_encrypted_private_key(self):
        key_path = BASE / "user.key"
        USER_KEY.write_private_key_file(str(key_path), password="key-passphrase")
        with patch.object(settings, "sftp_key_file", str(key_path)), patch.object(settings, "ftp_password", "key-passphrase"):
            self.assertTrue(ftpclient.test_connection()["ok"])

    def test_diagnostics_and_probe(self):
        result = ftpclient.test_connection()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["protocol"], "sftp")
        self.assertEqual(ftpprobe._open_n(2, 3), (2, []))
        self.assertEqual(ftpprobe._pick_file(3), ("/中文影片.mp4", len(PAYLOAD)))
        conn = ftpprobe._connect(3)
        try:
            self.assertEqual(ftpprobe._fetch_chunk(conn, "/中文影片.mp4", 12345, threading.Event()), 12345)
        finally:
            conn.close()

    def test_missing_file_and_bad_password(self):
        with self.assertRaises(ftpclient.FtpError):
            ftpclient.file_size("/missing.mp4")
        with self.assertRaises(ftpclient.FtpError):
            ftpclient.FtpReadStream("/missing.mp4")
        with patch.object(settings, "ftp_password", "wrong-secret"):
            result = ftpclient.test_connection()
            self.assertFalse(result["ok"])
            self.assertNotIn("wrong-secret", result["error"])

    def test_unknown_and_changed_host_keys_rejected(self):
        for key_text in ("", f"[127.0.0.1]:{settings.ftp_port} {self.key.get_name()} {paramiko.RSAKey.generate(2048).get_base64()}\n"):
            known = BASE / "untrusted_hosts"
            known.write_text(key_text)
            with patch.object(settings, "sftp_known_hosts", str(known)):
                self.assertFalse(ftpclient.test_connection()["ok"])

    def test_http_range(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.routers import stream
        app = FastAPI()
        app.include_router(stream.router)
        row = {"ftp_path": "/中文影片.mp4", "ext": "mp4", "size": len(PAYLOAD)}
        with patch.object(stream, "_file_or_404", return_value=row), TestClient(app) as client:
            response = client.get("/stream/1", headers={"Range": "bytes=123-567"})
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.headers["content-range"], f"bytes 123-567/{len(PAYLOAD)}")
            self.assertEqual(response.content, PAYLOAD[123:568])
            response = client.get("/stream/1", headers={"Range": "bytes=-77"})
            self.assertEqual(response.content, PAYLOAD[-77:])

    def test_ftp_legacy(self):
        from pyftpdlib.authorizers import DummyAuthorizer
        from pyftpdlib.handlers import FTPHandler
        from pyftpdlib.servers import FTPServer
        authorizer = DummyAuthorizer()
        authorizer.add_user("test", "test-password", str(MEDIA), perm="elr")
        class Handler(FTPHandler):
            pass
        Handler.authorizer = authorizer
        server = FTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"timeout": 0.1}, daemon=True)
        thread.start()
        try:
            with patch.object(settings, "ftp_protocol", "ftp"), patch.object(settings, "ftp_tls", False), patch.object(settings, "ftp_port", server.socket.getsockname()[1]):
                self.assertTrue(ftpclient.test_connection()["ok"])
                self.assertTrue(any(e.name == "中文影片.mp4" for e in ftpclient.list_dir("/")))
                self.assertEqual(ftpclient.file_size("/中文影片.mp4"), len(PAYLOAD))
                source = ftpclient.FtpReadStream("/中文影片.mp4", 123)
                self.assertEqual(b"".join(source.iter_chunks(limit=400)), PAYLOAD[123:523])
        finally:
            ftpclient.pool.close_all()
            server.close_all()
            thread.join(3)


USER_KEY = paramiko.RSAKey.generate(2048)

if __name__ == "__main__":
    try:
        result = unittest.main(verbosity=2, exit=False).result
    finally:
        from app import db
        conn = getattr(db._local, "conn", None)
        if conn is not None:
            conn.close()
        import gc
        gc.collect()
        TMP.cleanup()
    sys.exit(not result.wasSuccessful())
