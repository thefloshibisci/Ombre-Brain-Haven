"""Persistent media storage for Ombre Brain memories."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import ipaddress
import mimetypes
import os
import re
import socket
import stat
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

_SAFE_SUFFIX = re.compile(r"^\.[a-zA-Z0-9]{1,10}$")
_DEFAULT_MAX_BYTES = 25 * 1024 * 1024
_MAX_REDIRECTS = 3


class MediaPersistenceError(ValueError):
    """A media item could not be copied into persistent storage."""


class MediaStore:
    def __init__(
        self,
        vault_dir: str,
        media_dir: str | None = None,
        *,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.vault_dir = Path(vault_dir).resolve()
        self.media_dir = Path(media_dir or (self.vault_dir / "_media")).resolve()
        self.max_bytes = max(1, int(max_bytes))
        self.transport = transport
        self.media_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _suffix(name: str, mime_type: str) -> str:
        suffix = Path(name).suffix.lower()
        if _SAFE_SUFFIX.fullmatch(suffix):
            return suffix
        guessed = mimetypes.guess_extension((mime_type or "").split(";", 1)[0].strip()) or ".bin"
        return guessed if _SAFE_SUFFIX.fullmatch(guessed) else ".bin"

    def _stable_path(self, bucket_id: str, digest: str, suffix: str) -> Path:
        safe_bucket = re.sub(r"[^a-zA-Z0-9_.-]", "_", bucket_id)[:128]
        target_dir = (self.media_dir / safe_bucket).resolve()
        if self.media_dir not in target_dir.parents:
            raise MediaPersistenceError("媒体目录越界，已拒绝保存。")
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir / f"{digest}{suffix}"

    def _frontmatter_path(self, target: Path) -> str:
        try:
            return target.relative_to(self.vault_dir).as_posix()
        except ValueError:
            return str(target)

    @staticmethod
    def _atomic_write(target: Path, data: bytes) -> None:
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def _read_path(self, raw_path: str) -> tuple[bytes, str, str]:
        source = Path(raw_path).expanduser()
        try:
            before_open = os.lstat(source)
        except OSError as exc:
            raise MediaPersistenceError(
                f"媒体路径在 OB 服务器上不可读：{raw_path}。请改传 url 或 data_base64。"
            ) from exc
        if stat.S_ISLNK(before_open.st_mode) or not stat.S_ISREG(before_open.st_mode):
            raise MediaPersistenceError(f"媒体路径必须是普通文件且不能是符号链接：{raw_path}")
        if before_open.st_size > self.max_bytes:
            raise MediaPersistenceError(f"媒体文件超过单项上限 {self.max_bytes} 字节。")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(source, flags)
        try:
            opened = os.fstat(fd)
            after_open = os.lstat(source)
            if not stat.S_ISREG(opened.st_mode) or stat.S_ISLNK(after_open.st_mode):
                raise MediaPersistenceError("媒体路径在打开期间变得不安全。")
            if (after_open.st_dev, after_open.st_ino) != (opened.st_dev, opened.st_ino):
                raise MediaPersistenceError("媒体路径在打开期间发生变化。")
            with os.fdopen(fd, "rb") as handle:
                fd = -1
                data = handle.read(self.max_bytes + 1)
        finally:
            if fd >= 0:
                os.close(fd)
        if len(data) > self.max_bytes:
            raise MediaPersistenceError(f"媒体文件超过单项上限 {self.max_bytes} 字节。")
        return data, source.name, mimetypes.guess_type(source.name)[0] or ""

    def _decode_base64(self, value: str) -> tuple[bytes, str]:
        payload = value.strip()
        mime_type = ""
        if payload.startswith("data:"):
            header, separator, payload = payload.partition(",")
            if not separator:
                raise MediaPersistenceError("媒体 data URI 缺少数据部分。")
            if ";base64" not in header.lower():
                raise MediaPersistenceError("媒体 data URI 必须使用 Base64 编码。")
            mime_type = header[5:].split(";", 1)[0]
        try:
            data = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise MediaPersistenceError("媒体 data_base64 不是有效 Base64。") from exc
        if len(data) > self.max_bytes:
            raise MediaPersistenceError(f"媒体数据超过单项上限 {self.max_bytes} 字节。")
        return data, mime_type

    @staticmethod
    def _host_is_public(host: str) -> bool:
        if not host or host.lower() == "localhost":
            return False
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(host, None)}
        except OSError as exc:
            raise MediaPersistenceError(f"媒体链接域名无法解析：{host}") from exc
        if not addresses:
            return False
        for address in addresses:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
            if not ip.is_global:
                return False
        return True

    @classmethod
    def _validate_url(cls, value: str) -> str:
        if len(value) > 4000:
            raise MediaPersistenceError("媒体链接过长。")
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise MediaPersistenceError("媒体链接只接受公开的 http/https URL。")
        if parsed.username or parsed.password or not cls._host_is_public(parsed.hostname):
            raise MediaPersistenceError("媒体链接不能指向本机、内网或携带登录信息。")
        return value

    async def _download_url(self, raw_url: str) -> tuple[bytes, str, str, str]:
        url = self._validate_url(raw_url)
        async with httpx.AsyncClient(
            transport=self.transport,
            follow_redirects=False,
            timeout=httpx.Timeout(20.0, connect=10.0),
            trust_env=False,
            headers={"User-Agent": "Ombre-Brain-Media/1.0"},
        ) as client:
            for _ in range(_MAX_REDIRECTS + 1):
                async with client.stream("GET", url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise MediaPersistenceError("媒体链接重定向缺少目标地址。")
                        url = self._validate_url(urljoin(url, location))
                        continue
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        raise MediaPersistenceError(f"媒体链接下载失败：HTTP {response.status_code}") from exc
                    length = response.headers.get("content-length")
                    if length:
                        try:
                            declared_length = int(length)
                        except (TypeError, ValueError) as exc:
                            raise MediaPersistenceError("媒体链接返回了无效的 Content-Length。") from exc
                        if declared_length < 0:
                            raise MediaPersistenceError("媒体链接返回了无效的 Content-Length。")
                        if declared_length > self.max_bytes:
                            raise MediaPersistenceError(f"媒体文件超过单项上限 {self.max_bytes} 字节。")
                    chunks = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise MediaPersistenceError(f"媒体文件超过单项上限 {self.max_bytes} 字节。")
                        chunks.append(chunk)
                    name = Path(urlparse(url).path).name or "media"
                    return b"".join(chunks), name, response.headers.get("content-type", ""), url
            raise MediaPersistenceError("媒体链接重定向次数过多。")

    async def _persist_one(self, bucket_id: str, item: Any) -> tuple[dict[str, Any], Path | None]:
        if isinstance(item, str):
            entry = (
                {"url": item}
                if item.strip().lower().startswith(("http://", "https://"))
                else {"path": item}
            )
        elif isinstance(item, dict):
            entry = dict(item)
        else:
            raise MediaPersistenceError("media 每项必须是 URL、服务器路径或对象。")
        declared_type = str(entry.get("type") or entry.get("mime_type") or "")[:128]
        source_url = ""
        if entry.get("data_base64"):
            data, detected_type = self._decode_base64(str(entry["data_base64"]))
            source_name = str(entry.get("filename") or entry.get("title") or "media")
        elif entry.get("url"):
            data, source_name, detected_type, source_url = await self._download_url(str(entry["url"]).strip())
        else:
            raw_path = str(entry.get("path") or "").strip()
            if not raw_path:
                raise MediaPersistenceError("media 每项必须提供 url、path 或 data_base64。")
            data, source_name, detected_type = await asyncio.to_thread(self._read_path, raw_path)
        mime_type = (declared_type or detected_type or mimetypes.guess_type(source_name)[0] or "application/octet-stream").split(";", 1)[0]
        digest = hashlib.sha256(data).hexdigest()
        target = self._stable_path(bucket_id, digest, self._suffix(source_name, mime_type))
        created_path = None
        if not target.exists():
            await asyncio.to_thread(self._atomic_write, target, data)
            created_path = target
        result: dict[str, Any] = {
            "path": self._frontmatter_path(target),
            "sha256": digest,
            "size": len(data),
            "type": mime_type[:128],
            "stored": True,
        }
        if source_url:
            result["source_url"] = source_url[:2000]
        for key, limit in (("title", 200), ("note", 500)):
            if entry.get(key):
                result[key] = str(entry[key])[:limit]
        return result, created_path

    async def persist(self, bucket_id: str, media: Any) -> list[dict[str, Any]]:
        if not media:
            return []
        items = media if isinstance(media, list) else [media]
        if len(items) > 20:
            raise MediaPersistenceError("单条记忆最多保存 20 个媒体项。")
        results: list[dict[str, Any]] = []
        created_paths: list[Path] = []
        try:
            for item in items:
                result, created_path = await self._persist_one(bucket_id, item)
                results.append(result)
                if created_path is not None:
                    created_paths.append(created_path)
        except Exception:
            # A multi-item Miss is all-or-nothing. Do not leave the first files
            # behind when a later URL/path/base64 item fails.
            for path in reversed(created_paths):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
        # One physical image appears only once in a memory, even if callers
        # repeat it in the same hold/trace request.
        deduplicated: dict[str, dict[str, Any]] = {}
        for item in results:
            digest = str(item.get("sha256") or "")
            if digest in deduplicated:
                deduplicated[digest].update(item)
            else:
                deduplicated[digest] = item
        return list(deduplicated.values())

    def resolve(self, bucket_id: str, reference: dict) -> Path | None:
        raw_path = str((reference or {}).get("path") or "")
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = self.vault_dir / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            return None
        expected_dir = (self.media_dir / re.sub(r"[^a-zA-Z0-9_.-]", "_", bucket_id)[:128]).resolve()
        if expected_dir not in resolved.parents or not resolved.is_file():
            return None
        return resolved

    def cleanup(self, bucket_id: str, retained: Any = None) -> None:
        """Remove persisted files no longer referenced by this bucket."""
        safe_bucket = re.sub(r"[^a-zA-Z0-9_.-]", "_", bucket_id)[:128]
        bucket_dir = (self.media_dir / safe_bucket).resolve()
        if self.media_dir not in bucket_dir.parents or not bucket_dir.is_dir():
            return
        retained_paths = set()
        for reference in retained if isinstance(retained, list) else []:
            if not isinstance(reference, dict):
                continue
            path = self.resolve(bucket_id, reference)
            if path is not None:
                retained_paths.add(path)
        for path in bucket_dir.iterdir():
            try:
                if path.is_file() and not path.is_symlink() and path.resolve() not in retained_paths:
                    path.unlink()
            except OSError:
                pass
        try:
            bucket_dir.rmdir()
        except OSError:
            pass
