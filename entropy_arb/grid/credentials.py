"""Runtime-owned encrypted credentials. No legacy .env or console database.

The local key and ciphertext require a backup together, outside public/static
directories. Encryption protects accidental database/export disclosure; access
to both files as the runtime OS user can decrypt them.
"""
from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from pathlib import Path


class CredentialError(Exception):
    def __init__(self, code, message, status=409):
        self.code, self.message, self.status = code, message, status
        super().__init__(code)  # never interpolate submitted keys into errors


def _private_file(path: Path, *, create=False):
    flags = os.O_RDWR | os.O_NOFOLLOW
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        mode = os.fstat(fd)
        if not stat.S_ISREG(mode.st_mode) or mode.st_mode & 0o077 or mode.st_uid != os.getuid():
            raise CredentialError("VAULT_PERMISSIONS", "凭证文件必须由当前用户持有且权限为 600")
        return os.fdopen(fd, "r+b")
    except BaseException:
        os.close(fd)
        raise


class CredentialVault:
    AAD = b"entropy-grid-credentials:v1"

    def __init__(self, directory):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        self.directory = Path(directory).absolute()
        self._lease = None
        self.records = {}
        self.receipts = {}
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise CredentialError("VAULT_PERMISSIONS", "凭证目录必须由当前用户持有且权限为 700，不能是符号链接")
        self.path = self.directory / "accounts.enc"
        try:
            lock_path = self.directory / "writer.lock"
            try:
                self._lease = _private_file(lock_path, create=True)
            except FileExistsError:
                self._lease = _private_file(lock_path)
            try:
                fcntl.flock(self._lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise CredentialError("VAULT_IN_USE", "另一个网格服务正在管理此凭证目录") from None
            key_path = self.directory / "master.key"
            if not key_path.exists() and self.path.exists():
                raise CredentialError("VAULT_KEY_MISSING", "加密文件存在但密钥缺失；请恢复备份，不能重新生成")
            try:
                key_file = _private_file(key_path, create=True)
                with key_file:
                    key = AESGCM.generate_key(bit_length=256)
                    key_file.write(key)
                    key_file.flush()
                    os.fsync(key_file.fileno())
                self._sync_directory()
            except FileExistsError:
                with _private_file(key_path) as key_file:
                    key = key_file.read()
            if len(key) != 32:
                raise CredentialError("VAULT_KEY_INVALID", "凭证主密钥损坏；请恢复备份")
            self._cipher = AESGCM(key)
            if self.path.exists() or self.path.is_symlink():
                try:
                    with _private_file(self.path) as file:
                        data = file.read(4 * 1024 * 1024 + 1)
                    if len(data) > 4 * 1024 * 1024 or data[:4] != b"EGV1":
                        raise ValueError()
                    value = json.loads(self._cipher.decrypt(data[4:16], data[16:], self.AAD))
                    if value["version"] != 1 or not isinstance(value["records"], dict) or not isinstance(value["receipts"], dict):
                        raise ValueError()
                    self.records, self.receipts = value["records"], value["receipts"]
                except CredentialError:
                    raise
                except Exception:
                    raise CredentialError("VAULT_UNREADABLE", "凭证解密失败；请检查主密钥或恢复完整备份") from None
        except BaseException:
            self.close()
            raise

    def _sync_directory(self):
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def commit(self, records, receipts=None):
        """Commit atomically, keeping in-memory state unchanged on failure."""
        receipts = self.receipts if receipts is None else receipts
        plain = json.dumps({"version": 1, "records": records, "receipts": receipts},
                           ensure_ascii=False, separators=(",", ":")).encode()
        if len(plain) + 32 > 4 * 1024 * 1024:
            raise CredentialError("VAULT_FULL", "凭证库已达到容量限制，请联系维护人员归档请求记录")
        nonce = os.urandom(12)
        data = b"EGV1" + nonce + self._cipher.encrypt(nonce, plain, self.AAD)
        fd, temp = tempfile.mkstemp(prefix=".accounts-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp, self.path)
            self._sync_directory()
            self.records, self.receipts = records, receipts
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def close(self):
        if self._lease:
            self._lease.close()
            self._lease = None
