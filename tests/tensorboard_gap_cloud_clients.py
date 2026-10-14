"""Loopback HTTP protocols -> genuine provider SDKs -> native cloud readers.

Architecture: a temporary HTTP server exposes read-only S3/GCS/Azure response
formats -> real SDK signers, pagination and download streams -> native source
snapshots. Main callers: test_optional_tensorboard_gap_cloud_clients.py.
Why this exists: replacing filesystem providers bypasses their dependencies,
URL parsing, authentication headers and byte streaming. This fixture replaces
only the remote HTTP service. Its synthetic keys/tokens do not establish hosted
account access, credential discovery, OAuth refresh or provider permissions.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import ssl
import threading
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlsplit
from xml.sax.saxutils import escape


KEY = b"tensorboard-fixture-secret"
ACCOUNT = "fixture"
TLS_CA = Path(__file__).with_name("framework_init_tls_cert.pem")
TLS_KEY = Path(__file__).with_name("framework_init_tls_key.pem")


def verify_aws_signature(method, path, headers, *, access_key=ACCOUNT, secret_key=KEY,
                         service="s3", session_token=None, body=b""):
    """Independently recompute a real SDK SigV4 signature from wire fields.

    Main callers: ProtocolCloud and the TLS refresh fixture's STS/S3 handlers.
    Why this exists: credential rotation must change authenticated requests,
    rather than merely change a test object's token attribute.
    """
    authorization = headers.get("Authorization", "")
    prefix = "AWS4-HMAC-SHA256 "
    if not authorization.startswith(prefix):
        raise AssertionError("Missing fixture AWS SigV4 signature")
    fields = dict(part.split("=", 1) for part in authorization[len(prefix):].split(", "))
    credential = fields["Credential"].split("/")
    if credential[0] != access_key or credential[3:] != [service, "aws4_request"]:
        raise AssertionError("Wrong fixture AWS credential scope")
    lower = {name.lower(): value for name, value in headers.items()}
    if lower.get("x-amz-security-token") != session_token:
        raise AssertionError("Wrong fixture AWS session token")
    names = fields["SignedHeaders"].split(";")
    canonical_headers = "".join(name + ":" + " ".join(lower[name].split()) + "\n" for name in names)
    parsed = urlsplit(path)
    query = "&".join(quote(name, safe="-_.~") + "=" + quote(value, safe="-_.~")
                     for name, value in sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    canonical = "\n".join((method, parsed.path, query, canonical_headers, fields["SignedHeaders"],
                            lower.get("x-amz-content-sha256", hashlib.sha256(body).hexdigest())))
    scope = "/".join(credential[1:])
    string = "\n".join(("AWS4-HMAC-SHA256", lower["x-amz-date"], scope,
                          hashlib.sha256(canonical.encode()).hexdigest()))
    signing = b"AWS4" + secret_key
    for value in credential[1:]:
        signing = hmac.new(signing, value.encode(), hashlib.sha256).digest()
    expected = hmac.new(signing, string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(fields["Signature"], expected):
        raise AssertionError("Invalid fixture AWS SigV4 signature")
    return "AWS4-HMAC-SHA256"


class ProtocolCloud:
    """Serve actual SDK protocol requests with independently checked signatures.

    Main callers: provider_client and cloud-client tests. Listings paginate one
    object at a time, ranges are inclusive, generations/ETags protect changes,
    and all write methods fail. Request evidence excludes authentication secrets.
    """
    def __init__(self, provider, objects=None, label="protocol", *, tls=False, host="127.0.0.1", tls_cert=TLS_CA, tls_key=TLS_KEY):
        if host != "127.0.0.1":
            raise ValueError("Cloud protocol fixtures require a loopback address")
        self.tls, self.host = tls, host
        self.tls_cert, self.tls_key = tls_cert, tls_key
        self.provider = provider
        self.label = label
        self.objects = dict(objects or {})
        self.requests = []
        self.errors = []
        self.denied = False
        self.deny_reads = False
        self.bad_md5 = False
        self.truncate = False
        self.generation = 1
        self.server = None
        self.source_evidence = []

    def capture_sources(self):
        """Retain each tested source version before later fixture mutation.

        Main callers: running and real-event append tests. Small protobuf event
        files retain exact bytes; large deterministic stream tests retain size
        and SHA256, with their byte construction defined in the calling test.
        """
        self.source_evidence.append({name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                                           **({"bytes_hex": data.hex()} if len(data) < 65536 else {})}
                                     for name, data in self.objects.items()})

    def _authentication(self, method, path, headers):
        """Check synthetic bearer or recompute AWS/Azure request signatures.

        Main caller: the HTTP handler. The SDK is responsible for signing, and
        the fixture independently derives expected signatures from wire fields.
        """
        authorization = headers.get("Authorization", "")
        parsed = urlsplit(path)
        if self.provider == "gs":
            if authorization != "Bearer tensorboard-fixture-token":
                raise AssertionError("Missing fixture GCS bearer token")
            return "Bearer"
        lower = {name.lower(): value for name, value in headers.items()}
        if self.provider == "s3":
            return verify_aws_signature(method, path, headers)
        names = ("content-encoding", "content-language", "content-length", "content-md5", "content-type", "date",
                 "if-modified-since", "if-match", "if-none-match", "if-unmodified-since", "range")
        values = ["" if name == "content-length" and lower.get(name) == "0" else lower.get(name, "") for name in names]
        canonical_headers = "".join(name + ":" + value + "\n" for name, value in sorted(lower.items())
                                    if name.startswith("x-ms-"))
        query = "".join("\n" + name.lower() + ":" + ",".join(sorted(values))
                        for name, values in sorted(parse_qs(parsed.query, keep_blank_values=True).items()))
        canonical = method + "\n" + "\n".join(values) + "\n" + canonical_headers + "/" + ACCOUNT + parsed.path + query
        expected = base64.b64encode(hmac.new(KEY, canonical.encode(), hashlib.sha256).digest()).decode()
        if not hmac.compare_digest(authorization, "SharedKey " + ACCOUNT + ":" + expected):
            raise AssertionError("Invalid fixture Azure SharedKey signature")
        return "SharedKey"

    def _wire_response(self, method, path, headers, body):
        """Keep object reads separate from optional token-exchange endpoints.

        Main caller: the server handler. The TLS refresh subclass adds only
        explicit credential endpoints; unknown object writes remain errors.
        """
        if method not in {"GET", "HEAD"} or body:
            raise ValueError("Cloud source fixture accepts only read requests")
        mode = self._authentication(method, path, headers)
        self.requests.append({"method": method, "path": path, "authentication": mode,
                              "range": headers.get("Range") or headers.get("x-ms-range"),
                              "if_match": headers.get("If-Match")})
        return self._reply(method, path, headers)

    def _error(self, status, code):
        if self.provider == "gs":
            return status, {"content-type": "application/json"}, json.dumps({"error": {"code": status, "message": code}}).encode()
        return status, {"content-type": "application/xml", "x-ms-error-code": code}, (
            "<Error><Code>" + code + "</Code><Message>fixture</Message></Error>").encode()

    def _reply(self, method, path, headers):
        parsed = urlsplit(path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        if self.denied:
            return self._error(403, "AccessDenied" if self.provider == "s3" else "AuthorizationFailure")
        if self.provider == "gs" and "/o" not in parsed.path:
            return 200, {"content-type": "application/json"}, json.dumps({"name": ACCOUNT, "location": "US"}).encode()
        listing = ((self.provider == "s3" and "list-type" in query) or
                   (self.provider == "gs" and parsed.path.endswith("/o")) or
                   (self.provider == "az" and query.get("comp") == ["list"]))
        if listing:
            prefix = query.get("prefix", [""])[0]
            token_name = {"s3": "continuation-token", "gs": "pageToken", "az": "marker"}[self.provider]
            offset = int(query.get(token_name, ["0"])[0])
            names = sorted(name for name in self.objects if name.startswith(prefix))
            selected = names[offset:offset + 1]
            token = str(offset + 1) if offset + 1 < len(names) else ""
            if self.provider == "gs":
                result = {"items": [self._metadata(name) for name in selected]}
                if token:
                    result["nextPageToken"] = token
                return 200, {"content-type": "application/json"}, json.dumps(result).encode()
            if self.provider == "s3":
                items = "".join("<Contents><Key>" + escape(name) + "</Key><LastModified>2026-10-07T00:00:00Z</LastModified>"
                                '<ETag>"fixture"</ETag><Size>' + str(len(self.objects[name])) + "</Size></Contents>" for name in selected)
                data = '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>fixture</Name><Prefix>' + escape(prefix) + "</Prefix><IsTruncated>" + str(bool(token)).lower() + "</IsTruncated>" + items
                if token:
                    data += "<NextContinuationToken>" + token + "</NextContinuationToken>"
                return 200, {"content-type": "application/xml"}, (data + "</ListBucketResult>").encode()
            items = "".join("<Blob><Name>" + escape(name) + "</Name><Properties><Last-Modified>Wed, 07 Oct 2026 00:00:00 GMT</Last-Modified>"
                            '<Etag>"fixture-' + str(self.generation) + '"</Etag><Content-Length>' + str(len(self.objects[name])) + "</Content-Length><Content-Type>application/octet-stream</Content-Type><BlobType>BlockBlob</BlobType></Properties></Blob>" for name in selected)
            data = '<EnumerationResults ServiceEndpoint="' + self.endpoint + '" ContainerName="fixture"><Prefix>' + escape(prefix) + "</Prefix><Blobs>" + items + "</Blobs><NextMarker>" + token + "</NextMarker></EnumerationResults>"
            return 200, {"content-type": "application/xml"}, data.encode()
        key = unquote(parsed.path.split("/o/", 1)[1]) if self.provider == "gs" else unquote(parsed.path.split("/", 3 if self.provider == "az" else 2)[-1])
        if self.deny_reads:
            return self._error(403, "AccessDenied" if self.provider == "s3" else "AuthorizationFailure")
        if key not in self.objects:
            return self._error(404, "NoSuchKey" if self.provider == "s3" else "BlobNotFound")
        if self.provider == "gs" and "/download/" not in parsed.path:
            return 200, {"content-type": "application/json"}, json.dumps(self._metadata(key)).encode()
        if query.get("ifGenerationMatch", [str(self.generation)])[0] != str(self.generation):
            return self._error(412, "ConditionNotMet")
        etag = '"fixture-' + str(self.generation) + '"'
        if headers.get("If-Match", etag) != etag:
            return self._error(412, "ConditionNotMet")
        data = self.objects[key]
        result = {"content-type": "application/octet-stream", "etag": etag,
                  "last-modified": "Wed, 07 Oct 2026 00:00:00 GMT", "x-ms-blob-type": "BlockBlob",
                  "x-ms-request-id": "fixture", "x-goog-generation": str(self.generation)}
        range_value = headers.get("Range") or headers.get("x-ms-range")
        status = 200
        if range_value:
            start, end = range_value.split("=", 1)[1].split("-", 1)
            start = int(start)
            end = min(int(end), len(data) - 1) if end else len(data) - 1
            if start >= len(data):
                return self._error(416, "InvalidRange")
            result["content-range"] = f"bytes {start}-{end}/{len(data)}"
            data = data[start:end + 1]
            status = 206
        result["content-length"] = str(len(data))
        return status, result, data[:19] if self.truncate else data

    def _metadata(self, name):
        checksum = hashlib.md5(self.objects[name]).digest() if not self.bad_md5 else b"x" * 16
        return {"name": name, "bucket": ACCOUNT, "size": str(len(self.objects[name])),
                "generation": str(self.generation), "md5Hash": base64.b64encode(checksum).decode()}

    @contextmanager
    def running(self):
        """Bind loopback only and join its worker before returning from a test.

        Main callers: cloud-client tests. This temporary server cannot create or
        contact a hosted cloud account; no external hostname is used by clients.
        """
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.handle_read()

            def do_HEAD(self):
                self.handle_read()

            def do_POST(self):
                self.handle_read()

            def handle_read(self):
                try:
                    raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                    status, headers, body = owner._wire_response(self.command, self.path, self.headers, raw)
                except Exception as error:
                    owner.errors.append(str(error))
                    status, headers, body = owner._error(400, "InvalidFixtureRequest")
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                if "content-length" not in headers:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    try:
                        for offset in range(0, len(body), 8192):
                            self.wfile.write(body[offset:offset + 8192])
                    except (BrokenPipeError, ConnectionResetError):
                        pass
        self.server = ThreadingHTTPServer((self.host, 0), Handler)
        self.server.daemon_threads = True
        if self.tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(self.tls_cert, self.tls_key)
            self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.endpoint = ("https://" if self.tls else "http://") + self.host + ":" + str(self.server.server_port)
        worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        worker.start()
        self.capture_sources()
        try:
            yield self
        finally:
            self.server.shutdown()
            self.server.server_close()
            worker.join(timeout=5)
            self.capture_sources()
            output = Path(os.environ.get("BACKPROP_TENSORBOARD_CLOUD_OUTPUT_DIR",
                                         str(Path(__file__).resolve().parents[1] / "test-results")))
            output.mkdir(parents=True, exist_ok=True)
            target = output / f"tensorboard-gap-cloud-clients-{self.provider}-{self.label}-raw.json"
            target.write_text(json.dumps({"fixture": "loopback " + ("TLS" if self.tls else "HTTP") + " with synthetic credentials", "provider": self.provider,
                                          "hosted_authentication": "unverified", "requests": self.requests,
                                          "fixture_errors": self.errors, "source_versions": self.source_evidence}, indent=2) + "\n")


def provider_client(fixture):
    """Construct an actual installed SDK with only explicit synthetic credentials.

    Main callers: cloud-client tests replacing cloud._client. No default
    credential chain runs, and all endpoint URLs use the loopback fixture.
    """
    if fixture.provider == "s3":
        import boto3
        from botocore.config import Config
        return boto3.client("s3", endpoint_url=fixture.endpoint, region_name="us-east-1",
                            aws_access_key_id=ACCOUNT, aws_secret_access_key=KEY.decode(),
                            config=Config(retries={"max_attempts": 0}, s3={"addressing_style": "path"}))
    if fixture.provider == "gs":
        from google.cloud import storage
        from google.oauth2.credentials import Credentials
        return storage.Client(project="tensorboard-fixture", credentials=Credentials(token="tensorboard-fixture-token"),
                              client_options={"api_endpoint": fixture.endpoint})
    from azure.storage.blob import BlobServiceClient
    return BlobServiceClient(fixture.endpoint + "/" + ACCOUNT,
                             credential={"account_name": ACCOUNT, "account_key": base64.b64encode(KEY).decode()},
                             max_single_get_size=65536, max_chunk_get_size=65536, retry_total=0)


def root(provider, prefix="logs"):
    base = provider + "://" + ACCOUNT + ("/" + ACCOUNT if provider == "az" else "")
    return base + ("/" + prefix if prefix else "/")
