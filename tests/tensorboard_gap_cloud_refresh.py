"""TLS token endpoints -> real credential caches -> authenticated source reads.

Architecture: a test-owned clock expires issued synthetic credentials; real
STS/OAuth/MSAL clients exchange credentials over verified loopback TLS; real
S3/GCS/Azure clients automatically refresh and read source bytes. The fixture
independently verifies wire signatures or bearer generations and retains only
nonsecret request evidence. Main callers: optional TensorBoard refresh tests.
Why this exists: constructing a client with a replacement token does not prove
its expiry/cache/refresh logic. No SDK credential or request method is replaced.
Only the clock, remote protocol and client-construction boundary are supplied.
Hosted IAM, consent, default credential discovery and account TLS are separate.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from tensorboard_gap_cloud_clients import ACCOUNT, KEY, ProtocolCloud, TLS_CA, verify_aws_signature


TENANT = "00000000-0000-0000-0000-000000000001"
CLIENT_ID = "00000000-0000-0000-0000-000000000002"
CLIENT_SECRET = "tensorboard-fixture-client-secret"
REFRESH_TOKEN = "tensorboard-fixture-refresh-token"
WRONG_CERT = Path(__file__).with_name("tensorboard_gap_tls_wrong_cert.pem")
WRONG_KEY = Path(__file__).with_name("tensorboard_gap_tls_wrong_key.pem")


class CredentialClock:
    """Advance provider expiry checks without sleeping or replacing refresh.

    Main callers: RefreshCloud and RefreshClients. Google exposes a UTC helper,
    botocore accepts time_fetcher, and Azure/MSAL use Python time.time. TLS's
    certificate verification still uses the real OpenSSL clock.
    """
    def __init__(self):
        self.now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)

    def advance(self):
        self.now += timedelta(hours=2)

    @contextmanager
    def controlling(self, provider):
        if provider == "gs":
            with patch("google.auth._helpers.utcnow", side_effect=lambda: self.now.replace(tzinfo=None)):
                yield
        elif provider == "az":
            with patch("time.time", side_effect=lambda: self.now.timestamp()):
                yield
        else:
            yield


class RefreshCloud(ProtocolCloud):
    """Issue rotating synthetic credentials and reject expired source requests.

    Main callers: refresh/TLS regressions. Auth endpoints accept only the exact
    fixture grant and never contact a hosted identity provider. Object writes
    remain rejected by ProtocolCloud; a denied refresh is an explicit reply.
    """
    def __init__(self, provider, objects, label="refresh", *, wrong_hostname=False):
        options = {"tls_cert": WRONG_CERT, "tls_key": WRONG_KEY} if wrong_hostname else {}
        super().__init__(provider, objects, label, tls=True, **options)
        self.clock = CredentialClock()
        self.issued = {}
        self.refresh_denied = False
        self.issue_expired = False
        self.last_generation = None

    def _issue(self):
        generation = len(self.issued) + 1
        lifetime = -30 if self.issue_expired else 3600
        value = {"generation": generation, "access_key": "fixture-access-" + str(generation),
                 "secret_key": KEY + b"-" + str(generation).encode(),
                 "session_token": "fixture-session-" + str(generation),
                 "bearer": "tensorboard-fixture-access-" + str(generation),
                 "expires": self.clock.now + timedelta(seconds=lifetime), "lifetime": lifetime}
        self.issued[generation] = value
        return value

    def _wire_response(self, method, path, headers, body):
        """Validate credential exchanges separately from read-only source I/O.

        Main caller: ProtocolCloud's TLS request handler. Check fixture grant
        values, issue one versioned reply, and record generation/expiry without
        recording secrets. All other requests follow the checked source path.
        """
        parsed = urlsplit(path)
        discovery = "/" + TENANT + "/v2.0/.well-known/openid-configuration"
        if self.provider == "az" and method == "GET" and parsed.path == discovery:
            self.requests.append({"method": method, "path": path, "purpose": "openid_discovery"})
            return 200, {"content-type": "application/json"}, json.dumps({
                "issuer": self.endpoint + "/" + TENANT + "/v2.0",
                "authorization_endpoint": self.endpoint + "/" + TENANT + "/oauth2/v2.0/authorize",
                "token_endpoint": self.endpoint + "/" + TENANT + "/oauth2/v2.0/token",
                "jwks_uri": self.endpoint + "/keys"}).encode()
        expected = {"s3": "/sts", "gs": "/oauth/token", "az": "/" + TENANT + "/oauth2/v2.0/token"}[self.provider]
        if method == "POST" and parsed.path == expected:
            form = parse_qs(body.decode())
            if self.provider == "s3":
                verify_aws_signature(method, path, headers, service="sts", body=body)
                if form.get("Action") != ["GetSessionToken"] or form.get("DurationSeconds") != ["3600"]:
                    raise AssertionError("Unexpected fixture STS grant")
            else:
                wanted = {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                          "grant_type": "refresh_token" if self.provider == "gs" else "client_credentials"}
                if self.provider == "gs":
                    wanted["refresh_token"] = REFRESH_TOKEN
                elif "https://storage.azure.com/.default" not in form.get("scope", [""])[0].split():
                    raise AssertionError("Unexpected Azure storage scope")
                if any(form.get(name) != [value] for name, value in wanted.items()):
                    raise AssertionError("Unexpected fixture OAuth grant")
            request = {"method": method, "path": path, "purpose": "credential_refresh",
                       "authentication": "verified SigV4" if self.provider == "s3" else "verified fixture grant",
                       "denied": self.refresh_denied}
            self.requests.append(request)
            if self.refresh_denied:
                if self.provider == "s3":
                    return 403, {"content-type": "application/xml"}, (
                        '<ErrorResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/"><Error><Type>Sender</Type>'
                        '<Code>AccessDenied</Code><Message>Fixture credential refresh denied</Message></Error>'
                        '<RequestId>fixture</RequestId></ErrorResponse>').encode()
                return 400, {"content-type": "application/json"}, json.dumps({
                    "error": "invalid_grant" if self.provider == "gs" else "invalid_client",
                    "error_description": "Fixture credential refresh denied"}).encode()
            value = self._issue()
            request.update(generation=value["generation"], expires=value["expires"].isoformat())
            if self.provider == "s3":
                response = '<GetSessionTokenResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/"><GetSessionTokenResult><Credentials>'
                response += "<AccessKeyId>" + value["access_key"] + "</AccessKeyId><SecretAccessKey>" + value["secret_key"].decode() + "</SecretAccessKey>"
                response += "<SessionToken>" + value["session_token"] + "</SessionToken><Expiration>" + value["expires"].isoformat() + "</Expiration>"
                response += "</Credentials></GetSessionTokenResult><ResponseMetadata><RequestId>fixture</RequestId></ResponseMetadata></GetSessionTokenResponse>"
                return 200, {"content-type": "application/xml"}, response.encode()
            return 200, {"content-type": "application/json"}, json.dumps({"access_token": value["bearer"],
                "expires_in": value["lifetime"], "token_type": "Bearer", "refresh_token": REFRESH_TOKEN}).encode()
        result = super()._wire_response(method, path, headers, body)
        self.requests[-1].update(purpose="source_read", credential_generation=self.last_generation)
        return result

    def _authentication(self, method, path, headers):
        """Verify that a source request uses a genuinely issued generation.

        Main caller: inherited source request handling. AWS signatures include
        the rotating session token; bearer headers must match an issued token.
        Expiry denial is a provider response, not a test assertion failure.
        """
        authorization = headers.get("Authorization", "")
        if self.provider == "s3":
            access_key = authorization.partition("Credential=")[2].partition("/")[0]
            value = next((v for v in self.issued.values() if v["access_key"] == access_key), None)
            if value is None:
                raise AssertionError("Unknown fixture AWS credential generation")
            mode = verify_aws_signature(method, path, headers, access_key=value["access_key"],
                                        secret_key=value["secret_key"], session_token=value["session_token"])
        else:
            value = next((v for v in self.issued.values() if authorization == "Bearer " + v["bearer"]), None)
            if value is None:
                raise AssertionError("Unknown fixture bearer credential generation")
            mode = "verified Bearer"
        self.last_generation = value["generation"]
        return mode

    def _reply(self, method, path, headers):
        if self.issued[self.last_generation]["expires"] <= self.clock.now:
            return self._error(403, "ExpiredToken" if self.provider == "s3" else "AuthorizationFailure")
        return super()._reply(method, path, headers)


class RefreshClients:
    """Keep genuine credentials alive while each adapter owns its SDK client.

    Main callers: tests patching only cloud._client. Source clients and their
    HTTP sessions close per operation; the test explicitly owns the shared
    credential lifetime. The SDK's credential caches and refresh calls remain
    intact. Instrumenting close records its real execution, not a substitute.
    """
    def __init__(self, fixture, *, trusted=True):
        self.fixture, self.clients = fixture, []
        self.verify = str(fixture.tls_cert) if trusted else True
        self.credential = self.sts = self.session = None
        if fixture.provider == "s3":
            import boto3
            import botocore.session
            from botocore.credentials import RefreshableCredentials
            from botocore.config import Config
            self.sts = boto3.client("sts", endpoint_url=fixture.endpoint + "/sts", region_name="us-east-1",
                aws_access_key_id=ACCOUNT, aws_secret_access_key=KEY.decode(), verify=self.verify,
                config=Config(retries={"max_attempts": 0}, connect_timeout=2, read_timeout=2, proxies={}))
            self.credential = RefreshableCredentials("fixture-old", "fixture-old", "fixture-old",
                fixture.clock.now - timedelta(seconds=1), self._aws_refresh, "tensorboard-fixture-sts",
                time_fetcher=lambda: fixture.clock.now)
            session = botocore.session.Session()
            session._credentials = self.credential
            self.session = boto3.Session(botocore_session=session)
        elif fixture.provider == "gs":
            from google.oauth2.credentials import Credentials
            self.credential = Credentials(token=None, refresh_token=REFRESH_TOKEN,
                token_uri=fixture.endpoint + "/oauth/token", client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET, scopes=["https://www.googleapis.com/auth/devstorage.read_only"])
        else:
            from azure.identity import ClientSecretCredential
            self.credential = ClientSecretCredential(TENANT, CLIENT_ID, CLIENT_SECRET,
                authority=fixture.endpoint, disable_instance_discovery=True,
                connection_verify=self.verify, retry_total=0, connection_timeout=2, read_timeout=2,
                use_env_settings=False)

    def _aws_refresh(self):
        """Exchange fixture STS credentials through the genuine signed client.

        Main caller: botocore RefreshableCredentials after its own expiry check.
        This supplier models the credential service boundary; botocore owns
        deciding when to invoke it and rejecting already expired responses.
        """
        response = self.sts.get_session_token(DurationSeconds=3600)["Credentials"]
        return {"access_key": response["AccessKeyId"], "secret_key": response["SecretAccessKey"],
                "token": response["SessionToken"], "expiry_time": response["Expiration"].isoformat()}

    def client(self, source=None):
        """Create one real source client sharing the owned credential cache.

        Main caller: native cloud.filesystem through its construction seam.
        Explicit loopback endpoints and disabled environment proxies keep all
        TLS/token traffic inside the fixture. Certificate checking stays on.
        """
        provider = self.fixture.provider
        if provider == "s3":
            from botocore.config import Config
            client = self.session.client("s3", endpoint_url=self.fixture.endpoint, region_name="us-east-1",
                verify=self.verify, config=Config(retries={"max_attempts": 0},
                connect_timeout=2, read_timeout=2, proxies={}, s3={"addressing_style": "path"}))
        elif provider == "gs":
            from google.cloud import storage
            client = storage.Client(project="tensorboard-fixture", credentials=self.credential,
                                    client_options={"api_endpoint": self.fixture.endpoint})
            client._http.verify = self.verify
            client._http.trust_env = False
            client._http._auth_request_session.verify = self.verify
            client._http._auth_request_session.trust_env = False
        else:
            from azure.storage.blob import BlobServiceClient
            client = BlobServiceClient(self.fixture.endpoint + "/" + ACCOUNT, credential=self.credential,
                connection_verify=self.verify, max_single_get_size=65536, max_chunk_get_size=65536,
                retry_total=0, connection_timeout=2, read_timeout=2, use_env_settings=False)
        client.close = Mock(wraps=client.close)
        self.clients.append(client)
        return client

    def close(self):
        if self.sts is not None:
            self.sts.close()
        if hasattr(self.credential, "close"):
            self.credential.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
