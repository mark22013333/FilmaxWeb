"""首次設定精靈的後端：把「看懂 .env」換成「照著做、每一步都有人幫你驗」。

設計依據（Jellyfin／Immich 的首次設定都是同一個形狀）：
  * 精靈只收「不填就跑不起來」的東西，其餘全留預設，進階項目留給〈系統參數〉。
  * 每一步填完立刻驗證（測試連線、驗證金鑰），不要等整份存完才發現第一步就錯了。
  * 錯誤要翻成「下一步該做什麼」，不是把例外字串原樣丟給人。

**這裡寫的是 .env 本身。**FTP／登入這類設定在 config.py 是啟動時讀一次的，
存進資料庫不會生效；而且改錯會讓人進不了後台（params.py 的 Tier 0），
所以它們本來就屬於 .env。精靈只是替使用者把那個檔案寫對。
"""
from __future__ import annotations

import ftplib
import json
import logging
import os
import re
import shutil
import socket
import ssl
import stat
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import params
from .config import BASE_DIR, ENV_FILE

log = logging.getLogger("filmax.setup")

# 精靈允許寫進 .env 的鍵。**白名單，不是「登記表裡有的都行」**：
# FFMPEG_PATH／OPENAVER_PATH 會被當成命令或程式碼執行（Tier 4），
# LIBRARY_LOCAL_ROOTS 會擴大服務讀得到的本機範圍 —— 這幾個不能透過網頁寫。
WIZARD_KEYS = (
    "FTP_PROTOCOL", "FTP_HOST", "FTP_PORT", "FTP_USER", "FTP_PASSWORD",
    "FTP_TLS", "FTP_PASSIVE", "FTP_ENCODING",
    "LIBRARY_ROOTS",
    "TMDB_API_KEY",
    "AUTH_ENABLED", "AUTH_PASSWORD", "VIEWER_PASSWORD", "COOKIE_SECURE",
    "PUBLIC_BASE_URL",
)
ROOT_KINDS = ("auto", "movie", "tv")
_ENV_KEY_RE = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=")


# --------------------------------------------------------------------------
# .env 讀寫
# --------------------------------------------------------------------------
def _quote(value: str) -> str:
    """寫成 python-dotenv 讀得回原值的樣子。密碼裡有 # 或空白很常見。"""
    if value == "":
        return ""
    if re.fullmatch(r"[\w./:@+\-|;,=]+", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_env(updates: Dict[str, str], path: Path = ENV_FILE) -> List[str]:
    """把鍵值寫進 .env：原地更新已有的行，沒有就附加在檔尾。回傳有改動的鍵。

    * 其他行（含註解、順序）一個字都不動。
    * 先備份成 .env.bak，再用暫存檔 + os.replace 換上去，寫到一半當機不會留下半個檔。
    """
    for k, v in updates.items():
        if "\n" in v or "\r" in v or "\x00" in v:
            raise ValueError(f"{k} 含有不能出現的換行字元")

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []

    pending = dict(updates)
    changed: List[str] = []
    out: List[str] = []
    for line in lines:
        m = _ENV_KEY_RE.match(line)
        if m and m.group(1) in pending:
            key = m.group(1)
            new = f"{key}={_quote(pending.pop(key))}"
            if new != line.strip():
                changed.append(key)
            out.append(new)
        else:
            out.append(line)
    if pending:
        if out and out[-1].strip():
            out.append("")
        out.append("# ---- 由設定精靈寫入 ----")
        for key, v in pending.items():
            out.append(f"{key}={_quote(v)}")
            changed.append(key)

    if not changed:
        return []
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return changed


def read_env_values(keys=WIZARD_KEYS) -> Dict[str, str]:
    """直接讀 .env 檔（不是行程環境變數）—— 精靈要顯示的是「檔案裡現在寫什麼」。"""
    from dotenv import dotenv_values
    try:
        vals = dotenv_values(ENV_FILE)
    except OSError:
        vals = {}
    return {k: (vals.get(k) or "") for k in keys}


# --------------------------------------------------------------------------
# 狀態
# --------------------------------------------------------------------------
def needs_setup() -> bool:
    """還沒設過片源。判準是 .env 裡有沒有 FTP_HOST —— 既有安裝一定有，
    所以升級後不會被精靈打擾；全新安裝一定沒有。"""
    return not read_env_values(("FTP_HOST",))["FTP_HOST"]


def check_ffmpeg() -> Dict[str, Any]:
    from .config import settings

    def one(cmd: str) -> Dict[str, Any]:
        exe = shutil.which(cmd)
        if not exe:
            return {"ok": False, "path": cmd}
        try:
            r = subprocess.run([exe, "-version"], capture_output=True, text=True,
                               timeout=10, encoding="utf-8", errors="replace")
            first = (r.stdout or "").splitlines()[0] if r.stdout else ""
            return {"ok": r.returncode == 0, "path": exe, "version": first[:80]}
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"ok": False, "path": exe, "error": str(e)}

    ff, fp = one(settings.ffmpeg), one(settings.ffprobe)
    hint = ""
    if not ff["ok"] or not fp["ok"]:
        hint = ("找不到 ffmpeg／ffprobe。雙擊「安裝.bat」會自動裝好；"
                "沒有它，mkv／HEVC 這類瀏覽器直接播不了的影片就無法轉碼。")
    return {"ffmpeg": ff, "ffprobe": fp, "ok": ff["ok"] and fp["ok"], "hint": hint}


def check_dependencies() -> Dict[str, Any]:
    out = {}
    try:
        import paramiko  # noqa: F401
        out["paramiko"] = True
    except ImportError:
        out["paramiko"] = False
    return out


# --------------------------------------------------------------------------
# 片源連線測試（用「還沒存」的草稿值測）
# --------------------------------------------------------------------------
class Source:
    """精靈表單上的片源欄位。只收測試連線需要的那幾項。"""

    def __init__(self, d: Dict[str, Any]):
        self.protocol = str(d.get("protocol") or "ftp").lower()
        self.host = str(d.get("host") or "").strip()
        self.user = str(d.get("user") or "").strip()
        self.password = str(d.get("password") or "")
        self.tls = bool(d.get("tls"))
        self.passive = d.get("passive", True) is not False
        self.encoding = str(d.get("encoding") or "utf-8").strip()
        try:
            self.port = int(d.get("port") or (22 if self.protocol == "sftp" else 21))
        except (TypeError, ValueError):
            self.port = 0
        self.timeout = 10

    def problems(self) -> Optional[str]:
        if self.protocol not in ("ftp", "sftp"):
            return "傳輸協定只能是 ftp 或 sftp"
        if not self.host:
            return "請填主機位址"
        if re.search(r"[\s/\\]", self.host) or "://" in self.host:
            return "主機位址只要填 IP 或名稱，不要加 ftp:// 或斜線"
        if not 1 <= self.port <= 65535:
            return "連接埠要是 1～65535 的數字"
        if self.password == params.MASK:
            return "密碼欄位是遮罩，請重新輸入"
        return None


def explain(exc: BaseException, src: Source) -> Dict[str, str]:
    """把例外翻成「出了什麼事 + 下一步」。回 {code, message, hint}。"""
    s = str(exc)
    where = f"{src.host}:{src.port}"
    if isinstance(exc, socket.gaierror):
        return {"code": "dns", "message": f"找不到主機「{src.host}」",
                "hint": "確認位址有沒有打錯；在同一台電腦上就填 127.0.0.1。"}
    if isinstance(exc, ConnectionRefusedError):
        return {"code": "refused", "message": f"{where} 拒絕連線",
                "hint": f"主機找得到，但那個連接埠沒有服務在聽。"
                        f"{'SFTP 通常是 22' if src.protocol == 'sftp' else 'FTP 通常是 21'}；"
                        "也可能是 FTP／SSH 伺服器根本沒開。"}
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return {"code": "timeout", "message": f"連 {where} 逾時",
                "hint": "位址可能打錯、對方關機，或防火牆擋住了這個連接埠。"}
    if isinstance(exc, ftplib.error_perm) and s.startswith("530"):
        return {"code": "auth", "message": "帳號或密碼不對（530）",
                "hint": "確認大小寫；要匿名登入就帳號填 anonymous、密碼留空。"}
    if isinstance(exc, ssl.SSLError):
        return {"code": "tls", "message": "TLS 加密協商失敗",
                "hint": "伺服器可能不支援 FTPS，把「使用 FTPS」關掉再試；"
                        "或它用的是自簽憑證。"}
    name = type(exc).__name__
    if name == "AuthenticationException":
        return {"code": "auth", "message": "帳號或密碼不對",
                "hint": "Windows OpenSSH 要用 Windows 帳號密碼，不是 Windows Hello 的 PIN。"}
    if name in ("BadHostKeyException", "SSHException") and (
            "known_hosts" in s or "not found" in s.lower() or "host key" in s.lower()):
        return {"code": "hostkey", "message": "這台主機的金鑰還沒被信任",
                "hint": "為了防止被中間人攔截，程式不會自動接受陌生主機。"
                        f"請在執行 FilmaxWeb 的同一個帳號下開一個終端機，執行："
                        f"sftp -P {src.port} {src.user or '帳號'}@{src.host}"
                        "　核對指紋後輸入 yes，再回來按「測試連線」。"}
    if isinstance(exc, ImportError):
        return {"code": "dep", "message": "缺少 SFTP 需要的套件 paramiko",
                "hint": "雙擊「安裝.bat」重新安裝套件後重新啟動。"}
    if isinstance(exc, ftplib.Error) or name.endswith("Exception") or isinstance(exc, OSError):
        return {"code": "other", "message": s[:200] or name, "hint": ""}
    return {"code": "other", "message": f"{name}: {s[:200]}", "hint": ""}


class _Session:
    """草稿片源的一次性連線。with 離開時一定關掉。"""

    def __init__(self, src: Source):
        self.src = src
        self._ftp = None
        self._ssh = None
        self._sftp = None

    def __enter__(self):
        s = self.src
        if s.protocol == "sftp":
            import paramiko
            ssh = paramiko.SSHClient()
            ssh.load_system_host_keys()           # 不自動接受未知金鑰
            ssh.connect(s.host, port=s.port, username=s.user,
                        password=s.password or None, allow_agent=False,
                        look_for_keys=False, timeout=s.timeout,
                        banner_timeout=s.timeout, auth_timeout=s.timeout)
            self._ssh = ssh
            self._sftp = ssh.open_sftp()
            self._sftp.get_channel().settimeout(s.timeout)
        else:
            ftp = ftplib.FTP_TLS(timeout=s.timeout) if s.tls else ftplib.FTP(timeout=s.timeout)
            ftp.encoding = s.encoding
            try:
                ftp.connect(s.host, s.port)
                ftp.login(s.user or "anonymous", s.password)
                if s.tls:
                    ftp.prot_p()
                ftp.set_pasv(s.passive)
            except BaseException:
                try:
                    ftp.close()
                except Exception:
                    pass
                raise
            self._ftp = ftp
        return self

    def __exit__(self, *_):
        for obj in (self._sftp, self._ssh, self._ftp):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass

    def hello(self) -> str:
        if self._ftp:
            return (self._ftp.getwelcome() or "")[:120]
        return (self._ssh.get_transport().remote_version or "")[:120]

    def pwd(self) -> str:
        return self._ftp.pwd() if self._ftp else self._sftp.normalize(".")

    def dirs(self, path: str) -> Tuple[List[str], int]:
        """path 底下的子資料夾名稱，以及影片檔數量。"""
        from .ftpclient import VIDEO_EXTS
        names: List[str] = []
        videos = 0
        if self._sftp:
            for a in self._sftp.listdir_attr(path):
                if stat.S_ISDIR(a.st_mode or 0):
                    names.append(a.filename)
                elif a.filename.rsplit(".", 1)[-1].lower() in VIDEO_EXTS:
                    videos += 1
        else:
            ftp = self._ftp
            try:
                entries = list(ftp.mlsd(path, facts=["type"]))
            except ftplib.Error:
                entries = None
            if entries is not None:
                for n, facts in entries:
                    t = facts.get("type", "")
                    if t == "dir":
                        names.append(n)
                    elif t == "file" and n.rsplit(".", 1)[-1].lower() in VIDEO_EXTS:
                        videos += 1
            else:
                lines: List[str] = []
                ftp.retrlines(f"LIST {path}", lines.append)
                for ln in lines:
                    parts = ln.split(None, 8)
                    if len(parts) < 9:
                        parts = ln.split(None, 3)           # Windows IIS 格式
                        if len(parts) == 4:
                            (names if parts[2] == "<DIR>" else []).append(parts[3])
                        continue
                    if ln.startswith("d"):
                        names.append(parts[8])
                    elif parts[8].rsplit(".", 1)[-1].lower() in VIDEO_EXTS:
                        videos += 1
        names = [n for n in names if n not in (".", "..")]
        return sorted(names, key=str.lower), videos


def test_source(data: Dict[str, Any]) -> Dict[str, Any]:
    src = Source(data)
    bad = src.problems()
    if bad:
        return {"ok": False, "code": "input", "message": bad, "hint": ""}
    try:
        with _Session(src) as s:
            try:
                root_dirs, videos = s.dirs("/")
            except Exception as e:                    # 連得上但列不出根目錄
                return {"ok": True, "welcome": s.hello(), "pwd": s.pwd(),
                        "dirs": [], "warn": f"連上了，但列不出根目錄：{e}"}
            return {"ok": True, "welcome": s.hello(), "pwd": s.pwd(),
                    "dirs": root_dirs[:200], "videos": videos}
    except Exception as e:
        log.info("精靈測試片源失敗：%s", type(e).__name__)
        return {"ok": False, **explain(e, src)}


def browse_source(data: Dict[str, Any], path: str) -> Dict[str, Any]:
    src = Source(data)
    bad = src.problems()
    if bad:
        return {"ok": False, "code": "input", "message": bad, "hint": ""}
    if any(c in path for c in ("\r", "\n", "\x00")):
        return {"ok": False, "code": "input", "message": "路徑含非法字元", "hint": ""}
    path = "/" + path.strip("/") if path.strip("/") else "/"
    try:
        with _Session(src) as s:
            names, videos = s.dirs(path)
        return {"ok": True, "path": path, "dirs": names[:500], "videos": videos}
    except Exception as e:
        return {"ok": False, **explain(e, src)}


# --------------------------------------------------------------------------
# TMDB
# --------------------------------------------------------------------------
def test_tmdb(key: str) -> Dict[str, Any]:
    key = (key or "").strip()
    if not key:
        return {"ok": False, "message": "請貼上金鑰"}
    if key == params.MASK:
        return {"ok": False, "message": "金鑰欄位是遮罩，請重新貼上"}
    if len(key) > 40 or not re.fullmatch(r"[A-Za-z0-9]{20,40}", key):
        if key.startswith("eyJ"):
            return {"ok": False, "message": "這看起來是「API 讀取存取權杖」（很長、eyJ 開頭）",
                    "hint": "請改貼同一頁上的「API 金鑰」，是 32 個英數字。"}
        return {"ok": False, "message": "格式不對", "hint": "TMDB 的 v3 API 金鑰是 32 個英數字。"}
    url = "https://api.themoviedb.org/3/configuration?" + urllib.parse.urlencode({"api_key": key})
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            json.load(r)
        return {"ok": True}
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return {"ok": False, "message": "TMDB 說這把金鑰無效（401）",
                    "hint": "確認複製完整、沒有多的空白；剛申請的金鑰有時要等幾分鐘。"}
        return {"ok": False, "message": f"TMDB 回應 HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "message": "連不上 TMDB",
                "hint": f"這台電腦可能沒有對外網路，或 TMDB 在你的網路被擋。（{type(e).__name__}）"}


# --------------------------------------------------------------------------
# 組出要寫進 .env 的值
# --------------------------------------------------------------------------
def build_updates(form: Dict[str, Any]) -> Tuple[Dict[str, str], Dict[str, str]]:
    """表單 → (要寫的鍵值, 欄位錯誤)。**整批驗證過才回，有任何錯誤就不寫。**"""
    upd: Dict[str, str] = {}
    err: Dict[str, str] = {}

    src_in = form.get("source")
    if src_in is not None:
        src = Source(src_in)
        bad = src.problems()
        if bad:
            err["source"] = bad
        else:
            upd.update({
                "FTP_PROTOCOL": src.protocol, "FTP_HOST": src.host,
                "FTP_PORT": str(src.port), "FTP_USER": src.user,
                "FTP_TLS": "true" if src.tls and src.protocol == "ftp" else "false",
                "FTP_PASSIVE": "true" if src.passive else "false",
                "FTP_ENCODING": src.encoding or "utf-8",
            })
            if src.password:                      # 留空 = 沿用 .env 既有的
                upd["FTP_PASSWORD"] = src.password
            elif src_in.get("clearPassword"):
                upd["FTP_PASSWORD"] = ""

    roots = form.get("roots")
    if roots is not None:
        chunks = []
        for r in roots:
            p = "/" + str(r.get("path") or "").strip().strip("/")
            k = str(r.get("kind") or "auto").lower()
            if ";" in p or "|" in p:
                err["roots"] = f"資料夾路徑不能含 ; 或 |：{p}"
                break
            if k not in ROOT_KINDS:
                err["roots"] = f"類型只能是 {' / '.join(ROOT_KINDS)}"
                break
            chunks.append(f"{p}|{k}")
        else:
            upd["LIBRARY_ROOTS"] = ";".join(chunks) if chunks else "/|auto"

    if "tmdbKey" in form and str(form["tmdbKey"] or "").strip():
        k = str(form["tmdbKey"]).strip()
        if not re.fullmatch(r"[A-Za-z0-9]{20,40}", k):
            err["tmdbKey"] = "TMDB v3 金鑰是 32 個英數字"
        else:
            upd["TMDB_API_KEY"] = k

    acc = form.get("access")
    if acc is not None:
        if acc.get("mode") == "private":
            upd["AUTH_ENABLED"] = "false"
        elif acc.get("mode") == "public":
            admin = str(acc.get("adminPassword") or "")
            viewer = str(acc.get("viewerPassword") or "")
            base = str(acc.get("publicBaseUrl") or "").strip().rstrip("/")
            if len(admin) < 8:
                err["adminPassword"] = "管理員密碼至少 8 個字元"
            if viewer and len(viewer) < 6:
                err["viewerPassword"] = "唯讀密碼至少 6 個字元"
            if viewer and viewer == admin:
                err["viewerPassword"] = "兩組密碼一定要不一樣"
            if base and not re.fullmatch(r"https?://[^\s/]+", base):
                err["publicBaseUrl"] = "對外網址要像 https://video.example.com，結尾不要斜線"
            if not any(k in err for k in ("adminPassword", "viewerPassword", "publicBaseUrl")):
                upd["AUTH_ENABLED"] = "true"
                upd["AUTH_PASSWORD"] = admin
                upd["VIEWER_PASSWORD"] = viewer
                if base:
                    upd["PUBLIC_BASE_URL"] = base
                    upd["COOKIE_SECURE"] = "true" if base.startswith("https://") else "false"
        else:
            err["access"] = "請選擇誰要看"

    # 最後一道：寫進去的每個鍵都要過登記表的驗證，跟啟動時 validate_env 同一把尺。
    for k, v in list(upd.items()):
        if k not in WIZARD_KEYS:
            err[k] = "不在精靈能寫的範圍"
            continue
        p = params.REGISTRY.get(k)
        if p and v != "":
            try:
                params.parse(p, v)
            except ValueError as e:
                err[k] = str(e)
    return ({} if err else upd), err


def restart_supported() -> bool:
    return os.name == "nt" and (BASE_DIR / "重新啟動.bat").exists()


def spawn_restart() -> None:
    """用既有的 重新啟動.bat 在新視窗重開：它會先停止再啟動，我們這個行程會被它殺掉。"""
    subprocess.Popen(
        ["cmd", "/c", "start", "", str(BASE_DIR / "重新啟動.bat")],
        cwd=str(BASE_DIR), creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
