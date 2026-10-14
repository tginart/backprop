"""Serve explicitly selected W&B and Foundry replies on loopback TLS only.

Architecture: test-owned TLS origin -> checked route/GraphQL operation -> real
SDK HTTP request -> original response and complete request transcript. Unknown
routes are rejected and retained as fixture failures. The bundled private key
is public test material for localhost, never a deployment credential.
Main callers: framework_init_transport_probe. Why this exists: replacing a
store or init method cannot establish actual authentication or HTTP failures.
"""
from contextlib import AbstractContextManager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import ssl
import threading
import time
from urllib.parse import unquote, urlsplit


class LocalProtocol(AbstractContextManager):
    """Keep a real transport fixture and its exact replies alive for one probe.

    Main callers: isolated transport probe scenarios. A rejected read, viewer
    lookup or run publication is selected separately so error stages cannot be
    confused. Successful operations preserve supplied identities and records.
    """
    def __init__(self, *, failure=None, status=403, diagnostic=None):
        self.failure, self.status = failure, status
        self.diagnostic = diagnostic
        self.requests, self.unknown, self.records = [], [], []
        self.release = threading.Event()
        self.rejections = 0
        owner = self

        class DenyExternal(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reject(self):
                owner.unknown.append({'method': self.command, 'nonlocal_route': self.path})
                self.send_error(502, 'Nonlocal transport is forbidden in this fixture')

            do_CONNECT = reject
            do_GET = reject
            do_POST = reject

        self.guard = ThreadingHTTPServer(('127.0.0.1', 0), DenyExternal)
        self.guard.daemon_threads = True
        self.proxy = 'http://127.0.0.1:' + str(self.guard.server_port)
        self.guard_thread = threading.Thread(target=self.guard.serve_forever, name='init-egress-deny', daemon=True)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.handle_request()

            def do_POST(self):
                self.handle_request()

            def handle_request(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = {'unparsed_body': raw.decode('utf-8', errors='replace')}
                path = urlsplit(self.path).path
                operation = None
                if path == '/graphql' and self.command == 'POST' and isinstance(body, dict):
                    operation = body.get('operationName')
                    if not operation:
                        match = re.search(r'\b(?:query|mutation)\s+(\w+)', body.get('query', ''))
                        operation = match.group(1) if match else None
                request = {'method': self.command, 'path': self.path,
                           'operation': operation, 'body': body,
                           'authorization': self.headers.get('Authorization'),
                           'received_at': time.monotonic()}
                owner.requests.append(request)
                try:
                    stage, response = owner.response(path, self.command, operation, body)
                except ValueError as error:
                    owner.unknown.append(request)
                    self.reply(400, {'errors': [{'message': str(error)}]})
                    return
                if stage == 'telemetry_unavailable':
                    self.reply(404, {'errors': [{'message': 'Telemetry export is unavailable in the local fixture'}]})
                    return
                if owner.failure == stage:
                    statuses = owner.status if isinstance(owner.status, list) else [owner.status]
                    status = statuses[min(owner.rejections, len(statuses) - 1)]
                    owner.rejections += 1
                    request['response_status'] = status
                    self.reply(status, {'errors': [{'message': owner.diagnostic or 'fixture rejected ' + stage}]})
                    return
                if owner.failure == 'blocked_' + stage:
                    owner.release.wait(10)
                if stage == 'publish':
                    owner.records.extend(body['records'])
                request['response_status'] = 200
                self.reply(200, response)

            def reply(self, status, response):
                payload = json.dumps(response).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                    # Deadline probes deliberately close an in-flight response.
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        root = Path(__file__).parent
        context.load_cert_chain(root / 'framework_init_tls_cert.pem', root / 'framework_init_tls_key.pem')
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.host = 'https://127.0.0.1:' + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, name='init-loopback-server', daemon=True)

    def response(self, path, method, operation, body):
        """Return one source-shaped response, rejecting every unknown route.

        Main caller: request handler. Pinned gql_gen.go defines these replies;
        native replies implement only the exact stream endpoints exercised.
        """
        if method == 'POST' and path in {'/sdk/otel/v1/metrics', '/sdk/otel/v1/logs'}:
            return 'telemetry_unavailable', {}
        if path == '/graphql' and method == 'POST':
            if operation == 'Viewer':
                return 'auth', {'data': {'viewer': {'id': 'viewer', 'entity': 'e',
                    'username': 'fixture', 'email': 'fixture@invalid.test', 'flags': '{}', 'teams': {'edges': []}}}}
            if operation == 'ServerInfo':
                return 'info', {'data': {'serverInfo': {'cliVersionInfo': '{}', 'latestLocalVersionInfo': None}}}
            if operation == 'ServerFeaturesQuery':
                return 'info', {'data': {'serverInfo': {'features': []}}}
            if operation == 'UpsertBucket':
                values = body.get('variables', {})
                return 'init', {'data': {'upsertBucket': {'bucket': {
                    'id': 'storage-' + values.get('name', 'run'), 'name': values.get('name'),
                    'displayName': values.get('displayName') or 'fixture-name',
                    'description': None, 'config': values.get('config') or '{}',
                    'sweepName': None, 'historyLineCount': 0,
                    'project': {'id': 'project', 'name': values.get('project'),
                                'entity': {'id': 'entity', 'name': values.get('entity')}}}, 'inserted': True}}}
        stream = '/api/v2/highScale/streams/datasets/ri.foundry.main.dataset.init-fixture/streams/master/'
        if path == stream + 'getEndOffsets' and method == 'GET':
            return 'read', {}
        if path == stream + 'publishRecords' and method == 'POST':
            return 'publish', {}
        if method == 'POST' and re.fullmatch('/api/v2/datasets/ri.foundry.main.dataset.init-media/files/blobs/[a-f0-9]{64}/[^/]+/upload', unquote(path)):
            return 'file', {}
        raise ValueError('Unknown local protocol route: ' + method + ' ' + path + ' ' + str(operation))

    def __enter__(self):
        self.guard_thread.start()
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.guard.shutdown()
        self.guard.server_close()
        self.thread.join(5)
        self.guard_thread.join(5)
        if self.thread.is_alive() or self.guard_thread.is_alive():
            raise RuntimeError('Local protocol server did not stop')
