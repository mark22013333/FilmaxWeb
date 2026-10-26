"""SFTP connection lifecycle; host keys must already be trusted in known_hosts."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from .config import settings


@contextmanager
def connection(timeout=None):
    import paramiko

    s = settings
    timeout = s.ftp_timeout if timeout is None else timeout
    ssh = paramiko.SSHClient()
    sftp = None
    try:
        ssh.load_system_host_keys()
        if s.sftp_known_hosts:
            ssh.load_host_keys(str(Path(s.sftp_known_hosts).expanduser()))
        ssh.connect(
            s.ftp_host, port=s.ftp_port, username=s.ftp_user,
            password=s.ftp_password or None,
            key_filename=str(Path(s.sftp_key_file).expanduser()) if s.sftp_key_file else None,
            allow_agent=False, look_for_keys=False,
            timeout=timeout, banner_timeout=timeout, auth_timeout=timeout,
            channel_timeout=timeout,
        )
        sftp = ssh.open_sftp()
        sftp.get_channel().settimeout(timeout)
        yield sftp, ssh
    finally:
        try:
            if sftp is not None:
                sftp.close()
        finally:
            ssh.close()
