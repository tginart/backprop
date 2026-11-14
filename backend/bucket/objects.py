"""
Purpose: Give the bucket backend one small immutable object-store interface.
Input: A gs://, s3:// or file:// installation URI.
Output: Create-only writes, reads and listings with retry-classified errors.
Note: Keys are relative to the installation prefix. No adapter overwrites or
    deletes an object, so writers need only object create and read access.
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path
import uuid

from backprop_backend.http_store import BackendError
from .config import parse_bucket_uri


DEFAULT_TIMEOUT_S = 60
UPLOAD_RETRY_DEADLINE_S = 60
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
TRANSPORT_ERRORS = frozenset({
    "google.auth.exceptions.TransportError",
    "botocore.exceptions.EndpointConnectionError",
    "botocore.exceptions.ConnectionClosedError",
    "botocore.exceptions.ConnectTimeoutError",
    "botocore.exceptions.ReadTimeoutError",
    "botocore.exceptions.ResponseStreamingError",
})


class BucketError(BackendError):
    """An object-store failure with an explicit retry classification."""


def open_object_store(uri: str):
    """
    Purpose: Select the object-store adapter from the URI scheme.
    Input: A gs://, s3:// or file:// installation URI.
    Output: A GcsObjects, S3Objects or FileObjects instance.
    """
    scheme, bucket, prefix = parse_bucket_uri(uri)
    if scheme == "gs":
        return GcsObjects(bucket, prefix)
    if scheme == "s3":
        return S3Objects(bucket, prefix)
    return FileObjects(Path(prefix))


def dependency(name: str, extra: str):
    """
    Purpose: Import an optional package and name the extra that provides it.
    Input: A module name and the backprop-foundry extra that installs it.
    Output: The module. A broken installed package raises its own error.
    """
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as error:
        if error.name and (name == error.name or name.startswith(error.name + ".")):
            raise ImportError(
                f"The bucket backend requires backprop-foundry[{extra}] ({name})"
            ) from error
        raise


def is_transport_failure(error: BaseException | None) -> bool:
    """
    Purpose: Find a network failure anywhere in an exception chain.
    Input: An exception from a cloud SDK.
    Output: True if a connection, timeout or transport error caused it.
    Note: requests and botocore errors are not builtin ConnectionErrors.
    """
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        name = type(error).__module__ + "." + type(error).__name__
        if (isinstance(error, (ConnectionError, TimeoutError)) or name in TRANSPORT_ERRORS
                or name.startswith(("requests.exceptions.", "urllib3.exceptions."))):
            return True
        error = error.__cause__ or error.__context__
    return False


def _bucket_error(error: Exception, action: str, status: int | None) -> BucketError:
    retryable = status in RETRYABLE_STATUS if status is not None else is_transport_failure(error)
    detail = f"HTTP {status}" if status is not None else type(error).__name__
    result = BucketError(f"Bucket {action} failed ({detail})", status_code=status,
                         retryable=retryable)
    result.__cause__ = error
    return result


class GcsObjects:
    """
    Purpose: Implement the object interface on Google Cloud Storage.
    Input: A bucket name and key prefix. Credentials come from ADC.
    Output: Generation-checked creates, reads and prefix listings.
    """

    def __init__(self, bucket: str, prefix: str, *, client=None) -> None:
        self.root = prefix + "/" if prefix else ""
        if client is None:
            storage = dependency("google.cloud.storage", "bucket")
            client = storage.Client()
        self._client = client
        self._bucket = client.bucket(bucket)
        self._exceptions = dependency("google.api_core.exceptions", "bucket")

    def put_create_only(self, key: str, *, data: bytes) -> bool:
        """
        Purpose: Create an object only if no object exists at the key.
        Input: A relative key and the complete bytes.
        Output: True if this call created the object, False if it existed.
        """
        blob = self._bucket.blob(self.root + key)
        try:
            blob.upload_from_string(data, if_generation_match=0, checksum="crc32c",
                                    timeout=DEFAULT_TIMEOUT_S, retry=_upload_retry())
        except self._exceptions.PreconditionFailed:
            return False
        except Exception as error:
            raise self._wrap(error, "upload") from None
        return True

    def put_content_addressed(self, key: str, *, data: bytes) -> None:
        self.put_create_only(key, data=data)

    def get(self, key: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> bytes:
        try:
            return self._bucket.blob(self.root + key).download_as_bytes(timeout=timeout_s)
        except self._exceptions.NotFound:
            raise FileNotFoundError(key) from None
        except Exception as error:
            raise self._wrap(error, "read") from None

    def list(self, prefix: str, *, delimiter: str | None = None,
             start_offset: str | None = None, max_results: int | None = None,
             timeout_s: float = DEFAULT_TIMEOUT_S) -> tuple[list[str], list[str]]:
        """
        Purpose: List keys and child prefixes in lexicographic order.
        Input: A relative prefix, an optional delimiter and inclusive start key.
        Output: Relative keys and relative child prefixes.
        """
        try:
            pages = self._client.list_blobs(
                self._bucket, prefix=self.root + prefix, delimiter=delimiter,
                start_offset=self.root + start_offset if start_offset else None,
                max_results=max_results, page_size=1000, timeout=timeout_s)
            keys = [blob.name[len(self.root):] for blob in pages]
            prefixes = sorted(name[len(self.root):] for name in pages.prefixes)
        except self._exceptions.NotFound as error:
            raise BucketError("Bucket list failed: the bucket does not exist",
                              status_code=404, retryable=False) from error
        except Exception as error:
            raise self._wrap(error, "list") from None
        return keys, prefixes

    def _wrap(self, error: Exception, action: str) -> BucketError:
        status = getattr(error, "code", None)
        return _bucket_error(error, action, status if isinstance(status, int) else None)


class S3Objects:
    """
    Purpose: Implement the object interface on Amazon S3.
    Input: A bucket name and key prefix. Credentials use the boto3 chain.
    Output: If-None-Match creates, reads and prefix listings.
    Note: S3-compatible stores that ignore If-None-Match break first-writer-wins.
    """

    def __init__(self, bucket: str, prefix: str, *, client=None) -> None:
        self.root = prefix + "/" if prefix else ""
        self.bucket = bucket
        self._client = client or dependency("boto3", "aws").client("s3")

    def put_create_only(self, key: str, *, data: bytes) -> bool:
        try:
            self._client.put_object(Bucket=self.bucket, Key=self.root + key, Body=data,
                                    IfNoneMatch="*")
        except Exception as error:
            if _s3_code(error) == "PreconditionFailed":
                return False
            raise self._wrap(error, "upload") from None
        return True

    def put_content_addressed(self, key: str, *, data: bytes) -> None:
        """
        Purpose: Store content-addressed bytes without a conditional multipart upload.
        Input: A key that contains the SHA-256 of the data.
        Output: The object exists. Identical bytes make a second upload harmless.
        """
        try:
            self._client.head_object(Bucket=self.bucket, Key=self.root + key)
            return
        except Exception as error:
            if _s3_status(error) != 404:
                raise self._wrap(error, "read") from None
        try:
            self._client.put_object(Bucket=self.bucket, Key=self.root + key, Body=data)
        except Exception as error:
            raise self._wrap(error, "upload") from None

    def get(self, key: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> bytes:
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=self.root + key)
            return response["Body"].read()
        except Exception as error:
            if _s3_code(error) in ("NoSuchKey", "404"):
                raise FileNotFoundError(key) from None
            raise self._wrap(error, "read") from None

    def list(self, prefix: str, *, delimiter: str | None = None,
             start_offset: str | None = None, max_results: int | None = None,
             timeout_s: float = DEFAULT_TIMEOUT_S) -> tuple[list[str], list[str]]:
        options = {"Bucket": self.bucket, "Prefix": self.root + prefix}
        if delimiter:
            options["Delimiter"] = delimiter
        if start_offset:
            # StartAfter is exclusive. Callers use a lookback, so one key is harmless.
            options["StartAfter"] = self.root + start_offset
        keys, prefixes = [], []
        try:
            for page in self._client.get_paginator("list_objects_v2").paginate(**options):
                keys.extend(item["Key"][len(self.root):] for item in page.get("Contents", []))
                prefixes.extend(item["Prefix"][len(self.root):]
                                for item in page.get("CommonPrefixes", []))
                if max_results is not None and len(keys) >= max_results:
                    break
        except Exception as error:
            raise self._wrap(error, "list") from None
        return keys[:max_results] if max_results else keys, sorted(prefixes)

    def _wrap(self, error: Exception, action: str) -> BucketError:
        if _s3_code(error) == "ConditionalRequestConflict":
            return BucketError("Bucket upload conflicted with a concurrent write",
                               status_code=409, retryable=True)
        return _bucket_error(error, action, _s3_status(error))


class FileObjects:
    """
    Purpose: Implement the object interface on a local directory for tests.
    Input: An absolute directory path.
    Output: Atomic create-only files and a flat sorted key space.
    """

    def __init__(self, root: Path) -> None:
        self.directory = root
        self.directory.mkdir(parents=True, exist_ok=True)

    def put_create_only(self, key: str, *, data: bytes) -> bool:
        """
        Purpose: Create a file only if it is absent, without a partial file.
        Input: A relative key and the complete bytes.
        Output: True if this call created the file.
        Method:
            1. Write the bytes to a temporary file in the target directory.
            2. Hard-link the temporary file to the target name. A link fails if
               the target exists, so the create is atomic.
        """
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f".{target.name}.tmp-{uuid.uuid4().hex}"
        try:
            temporary.write_bytes(data)
            os.link(temporary, target)
        except FileExistsError:
            return False
        finally:
            temporary.unlink(missing_ok=True)
        return True

    def put_content_addressed(self, key: str, *, data: bytes) -> None:
        self.put_create_only(key, data=data)

    def get(self, key: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> bytes:
        return self._path(key).read_bytes()

    def list(self, prefix: str, *, delimiter: str | None = None,
             start_offset: str | None = None, max_results: int | None = None,
             timeout_s: float = DEFAULT_TIMEOUT_S) -> tuple[list[str], list[str]]:
        names = []
        for path in self.directory.rglob("*"):
            if path.is_file() and ".tmp-" not in path.name:
                names.append(path.relative_to(self.directory).as_posix())
        keys, prefixes = [], set()
        for name in sorted(names):
            if not name.startswith(prefix) or (start_offset and name < start_offset):
                continue
            rest = name[len(prefix):]
            if delimiter and delimiter in rest:
                prefixes.add(prefix + rest.split(delimiter, 1)[0] + delimiter)
            else:
                keys.append(name)
        return keys[:max_results] if max_results else keys, sorted(prefixes)

    def _path(self, key: str) -> Path:
        parts = key.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise ValueError("Invalid bucket object key")
        return self.directory.joinpath(*parts)


def _upload_retry():
    # Every upload sets if_generation_match=0, so a plain retry is idempotent.
    # The key stays fixed during these retries. After the deadline the journal
    # retries with a new key, so readers never wait long for a stalled upload.
    from google.cloud.storage.retry import DEFAULT_RETRY
    return DEFAULT_RETRY.with_timeout(UPLOAD_RETRY_DEADLINE_S)


def _s3_status(error: Exception) -> int | None:
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return None
    return response.get("ResponseMetadata", {}).get("HTTPStatusCode")


def _s3_code(error: Exception) -> str | None:
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return None
    return response.get("Error", {}).get("Code")
