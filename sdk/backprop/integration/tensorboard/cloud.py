"""Cloud source URLs -> installed SDK clients -> bounded binary source reads.

Architecture: validate one pinned s3/gs/az address -> lazily create its provider
client -> enumerate object keys -> expose ordinary binary readers to the atomic
snapshot boundary. Local sources and framework writers remain separate.
Main callers: sources.filesystem, reader._files and sources.snapshot.
Why this exists: TensorFlow and TensorBoard's fallback have different cloud
plugins, and their Azure address shape differs from the pinned W&B core. These
read-only adapters use the already declared aws/gcp/azure optional dependencies.
They never create buckets, upload source objects or invent credential options.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import importlib
import ipaddress
import io
import os
from pathlib import PureWindowsPath
import ssl
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Address:
    scheme: str
    authority: str
    bucket: str
    key: str


def address(path):
    """Parse supported source forms without URL-decoding literal object names.

    Main callers: canonical_path and CloudFileSystem. Query credentials and
    endpoint options are outside the selected public signatures. Reject them
    explicitly instead of silently using them as part of an object key.
    """
    if isinstance(path, bytes):
        path = path.decode("utf-8")
    parts = urlsplit(path)
    if parts.scheme not in {"s3", "gs", "az"}:
        raise ValueError("Unsupported TensorBoard cloud scheme; expected s3, gs or az")
    if not parts.netloc or any(char in parts.netloc for char in "@:[]\\"):
        raise ValueError("TensorBoard cloud source requires a bucket or account without credentials or port")
    if parts.query or parts.fragment or "?" in path or "#" in path:
        raise ValueError("TensorBoard cloud source URL query and fragment options are unsupported")
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise ValueError("TensorBoard cloud source contains a control character")
    if "//" in parts.path or "\\" in parts.path:
        raise ValueError("TensorBoard cloud source contains an unsafe path component")
    key = parts.path.strip("/")
    if key and any(part in {"", ".", ".."} or PureWindowsPath(part).drive for part in key.split("/")):
        raise ValueError("TensorBoard cloud source contains an unsafe path component")
    if parts.scheme == "az":
        if not key:
            raise ValueError("Azure TensorBoard sources require az://account/container[/prefix]")
        bucket, _, key = key.partition("/")
    else:
        bucket = parts.netloc
    return Address(parts.scheme, parts.netloc, bucket, key)


def canonical_path(path):
    address(path)
    return path.rstrip("/")


def _dependency(name, extra):
    """Load only the selected provider and distinguish missing from broken SDKs.

    Main callers: _client. A broken installed dependency propagates its original
    error; missing packages identify the existing optional extra to install.
    """
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as error:
        if error.name and (name == error.name or name.startswith(error.name + ".")):
            raise ImportError(f"TensorBoard cloud reads require backprop-foundry[{extra}] ({name})") from error
        raise


def _tls_verification_failed(error):
    """Recognize certificate failures through original SDK transport causes.

    Main caller: _gcs_retry. Google wraps requests/urllib3 TLS errors in a
    TransportError that its default retry predicate treats as transient. Walk
    real exception links without changing or string-matching the source error.
    """
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        pending.extend((current.__cause__, current.__context__, getattr(current, "reason", None)))
        pending.extend(value for value in current.args if isinstance(value, BaseException))
    return False


def _gcs_retry():
    """Keep SDK transient retries while rejecting permanent certificate errors.

    Main callers: GCS listings, object metadata and downloads. Why this exists:
    retrying a rejected certificate cannot repair its trust or hostname, and
    delays the error while an import owns a temporary snapshot. Retain the
    original provider predicate, delays and deadline for genuine transients.
    """
    original = _dependency("google.cloud.storage.retry", "gcp").DEFAULT_RETRY
    return original.with_predicate(lambda error: not _tls_verification_failed(error) and original._predicate(error))


def _bool_environment(name):
    value = os.environ.get(name, "false").lower()
    if value not in {"true", "false", "1", "0"}:
        raise ValueError(f"Invalid boolean TensorBoard provider setting: {name}")
    return value in {"true", "1"}


def _azure_endpoint_account(endpoint):
    """Read an account embedded in a known Azure or local emulator endpoint.

    Main caller: _azure_client's connection-string validation. Why this exists:
    BlobServiceClient.account_name may be overwritten by credential AccountName,
    hiding a different account in BlobEndpoint. Custom DNS aliases expose no
    account identity here and require actual hosted authorization to verify.
    """
    parts = urlsplit(endpoint)
    if parts.scheme not in {"http", "https"}:
        raise ValueError("Invalid Azure TensorBoard connection-string endpoint protocol")
    hostname = (parts.hostname or "").lower()
    suffixes = (".blob.core.windows.net", ".blob.core.usgovcloudapi.net",
                ".blob.core.chinacloudapi.cn", ".blob.core.cloudapi.de")
    if hostname.endswith(suffixes):
        return hostname.split(".", 1)[0]
    try:
        ipaddress.ip_address(hostname)
        local = True
    except ValueError:
        local = hostname == "localhost" or hostname.endswith(".localhost")
    if local:
        return parts.path.strip("/").split("/", 1)[0]
    return None


def _azure_client(source):
    """Create Azure's real client with the pinned environment credential modes.

    Main caller: _client. Unlike the pinned Go opener, refuse an environment
    account that contradicts the source URL instead of reading a different
    account. Connection strings and SAS stay out of source identities.
    """
    sdk = _dependency("azure.storage.blob", "azure")
    account = os.environ.get("AZURE_STORAGE_ACCOUNT", source.authority)
    if account != source.authority:
        raise ValueError("Azure TensorBoard source account disagrees with AZURE_STORAGE_ACCOUNT")
    # Endpoint validation owns no credential or HTTP resources. Keep it before
    # both credential selection and construction, including connection strings.
    domain = os.environ.get("AZURE_STORAGE_DOMAIN", "blob.core.windows.net")
    protocol = os.environ.get("AZURE_STORAGE_PROTOCOL", "https")
    if protocol not in {"http", "https"}:
        raise ValueError("Invalid Azure TensorBoard storage protocol")
    local = _bool_environment("AZURE_STORAGE_IS_LOCAL_EMULATOR")
    cdn = _bool_environment("AZURE_STORAGE_IS_CDN")
    key = os.environ.get("AZURE_STORAGE_KEY")
    connection = os.environ.get("AZURE_STORAGE_CONNECTION_STRING") or os.environ.get("AZURE_STORAGEBLOB_CONNECTIONSTRING")
    sas = os.environ.get("AZURE_STORAGE_SAS_TOKEN")
    if key:
        credential = {"account_name": account, "account_key": key}
    elif sas:
        credential = sas
    elif connection:
        client = sdk.BlobServiceClient.from_connection_string(connection)
        try:
            endpoint_account = _azure_endpoint_account(client.url)
            if client.account_name != account or (endpoint_account is not None
                    and endpoint_account not in {account, account + "-secondary"}):
                raise ValueError("Azure TensorBoard source account disagrees with the connection string")
        except BaseException:
            client.close()
            raise
        return client
    else:
        credential = _dependency("azure.identity", "azure").DefaultAzureCredential()
    if local or domain.startswith(("127.0.0.1", "localhost")):
        endpoint = f"{protocol}://{domain}/{account}"
    elif cdn:
        endpoint = f"{protocol}://{domain}"
    else:
        endpoint = f"{protocol}://{account}.{domain}"
    try:
        client = sdk.BlobServiceClient(endpoint, credential=credential)
    except BaseException:
        if hasattr(credential, "close"):
            credential.close()
        raise
    if hasattr(credential, "close"):
        client._tensorboard_source_credential = credential
    return client


def _client(source):
    """Keep real SDK credential resolution at one replaceable construction seam.

    Main caller: CloudFileSystem. Protocol tests replace this construction seam
    with actual installed SDK clients using synthetic credentials and an HTTP
    fixture; provider methods and response parsers are never replaced.
    """
    if source.scheme == "s3":
        sdk = _dependency("boto3", "aws")
        endpoint = os.environ.get("S3_ENDPOINT")
        return sdk.client("s3", endpoint_url=endpoint) if endpoint else sdk.client("s3")
    if source.scheme == "gs":
        return _dependency("google.cloud.storage", "gcp").Client(project=None)
    return _azure_client(source)


class _Chunks(io.RawIOBase):
    """Turn Azure's genuine download iterator into a bounded binary reader.

    Main caller: CloudFileSystem.GFile. The snapshot asks for fixed-size reads;
    retain at most one SDK chunk plus the requested output between calls.
    """
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.pending = b""

    def readable(self):
        return True

    def read(self, size=-1):
        self._checkClosed()
        output = []
        remaining = size
        while remaining != 0:
            if not self.pending:
                self.pending = next(self.chunks, b"")
                if not self.pending:
                    break
            take = len(self.pending) if remaining < 0 else min(remaining, len(self.pending))
            output.append(self.pending[:take])
            self.pending = self.pending[take:]
            if remaining > 0:
                remaining -= take
        return b"".join(output)

    def close(self):
        close = getattr(self.chunks, "close", None)
        if close:
            close()
        self.pending = b""
        super().close()


class _CheckedReader:
    """Check complete binary reads before the snapshot can replace prior bytes.

    Main caller: CloudFileSystem.GFile. GCS range reads disable per-request
    checksums, so validate the complete object MD5 when metadata provides it.
    Generation and ETag conditions prevent combining different object revisions.
    """
    def __init__(self, reader, length, md5=None):
        self.reader, self.length, self.md5 = reader, length, md5
        self.count = 0
        self.digest = hashlib.md5() if md5 else None

    def read(self, size=-1):
        data = self.reader.read(size)
        if not isinstance(data, bytes):
            raise TypeError("TensorBoard cloud binary reads must return bytes")
        self.count += len(data)
        if self.digest:
            self.digest.update(data)
        if not data and size != 0:
            if self.count != self.length:
                raise OSError("TensorBoard cloud source length changed or download was incomplete")
            if self.digest and base64.b64encode(self.digest.digest()).decode() != self.md5:
                raise OSError("TensorBoard cloud source MD5 checksum mismatch")
        return data


class CloudFileSystem:
    """Present one SDK-backed cloud source as the reader's filesystem boundary.

    Main callers: sources.filesystem and reader._files. Listings are complete
    SDK paginated listings, then expressed as gfile-style directories. No writer
    or storage mutation methods are provided.
    """
    def __init__(self, root):
        self.source = address(root)
        self.client = _client(self.source)

    def close(self):
        """Release this adapter's SDK session and owned Azure credential client.

        Main caller: sources.open_filesystem on listing/download completion or
        failure. Each operation owns its adapter; no global credential cache is
        introduced and framework filesystem sessions are untouched.
        """
        credential = getattr(self.client, "_tensorboard_source_credential", None)
        try:
            close = getattr(self.client, "close", None)
            if close:
                close()
        finally:
            if credential is not None:
                credential.close()

    def _address(self, path):
        parsed = address(path)
        if (parsed.scheme, parsed.authority, parsed.bucket) != (self.source.scheme, self.source.authority, self.source.bucket):
            raise ValueError("TensorBoard cloud file belongs to another bucket or account")
        return parsed

    def _keys(self, path):
        parsed = self._address(path)
        prefix = parsed.key.rstrip("/") + "/" if parsed.key else ""
        if parsed.scheme == "s3":
            pages = self.client.get_paginator("list_objects_v2").paginate(Bucket=parsed.bucket, Prefix=prefix)
            return (item["Key"] for page in pages for item in page.get("Contents", []))
        if parsed.scheme == "gs":
            return (item.name for item in self.client.list_blobs(parsed.bucket, prefix=prefix, retry=_gcs_retry()))
        container = self.client.get_container_client(parsed.bucket)
        return (item.name for item in container.list_blobs(name_starts_with=prefix))

    def isdir(self, path):
        return next(iter(self._keys(path)), None) is not None

    def walk(self, root):
        """Build directory entries from a recursive, fully paginated key listing.

        Main caller: reader._files. Provider response order never determines
        physical event order; reader sorts the returned source paths as usual.
        """
        parsed = self._address(root)
        prefix = parsed.key.rstrip("/") + "/" if parsed.key else ""
        directories = {"": (set(), set())}
        for key in self._keys(root):
            if not key.startswith(prefix):
                raise ValueError("TensorBoard provider returned a key outside its requested prefix")
            relative = key[len(prefix):]
            if not relative or relative.endswith("/"):
                continue
            parts = relative.split("/")
            if any(part in {"", ".", ".."} for part in parts):
                raise ValueError("TensorBoard provider returned an unsafe object name")
            parent = ""
            for child in parts[:-1]:
                directories.setdefault(parent, (set(), set()))[0].add(child)
                parent = parent + "/" + child if parent else child
                directories.setdefault(parent, (set(), set()))
            directories[parent][1].add(parts[-1])
        for directory, (children, files) in sorted(directories.items()):
            path = root.rstrip("/") + ("/" + directory if directory else "")
            yield path, sorted(children), sorted(files)

    @contextmanager
    def GFile(self, path, mode):
        """Stream a real SDK download and close it on success or provider error.

        Main caller: sources.snapshot. Download failures propagate before atomic
        replacement. GCS pins generation; Azure pins the download's initial ETag
        across chunks; S3 uses one complete streaming HTTP response.
        """
        if mode != "rb":
            raise ValueError("TensorBoard cloud filesystem supports only binary source reads")
        parsed = self._address(path)
        if parsed.scheme == "s3":
            result = self.client.get_object(Bucket=parsed.bucket, Key=parsed.key)
            reader = result["Body"]
            checked = _CheckedReader(reader, result["ContentLength"])
        elif parsed.scheme == "gs":
            retry = _gcs_retry()
            blob = self.client.bucket(parsed.bucket).get_blob(parsed.key, retry=retry)
            if blob is None:
                raise FileNotFoundError("TensorBoard GCS source object does not exist")
            reader = blob.open("rb", chunk_size=1024 * 1024, if_generation_match=blob.generation, retry=retry)
            checked = _CheckedReader(reader, blob.size, blob.md5_hash)
        else:
            download = self.client.get_blob_client(parsed.bucket, parsed.key).download_blob()
            reader = _Chunks(download.chunks())
            checked = _CheckedReader(reader, download.size)
        try:
            yield checked
        finally:
            reader.close()
