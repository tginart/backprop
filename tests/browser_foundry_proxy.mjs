/**
 * Purpose: Test installed dashboard code against a fixture or a real Foundry API.
 * Input: A browser context, public configuration, and a server-side test token.
 * Output: Requests to the selected Foundry host with explicit credential boundaries.
 * Note: OAuth UI consent is covered by separate protocol tests. This proxy tests data access.
 */
export async function configureFoundryBrowser(context, {origin, config, token, project, initialOffsets}) {
    const host = config.host;
    const requests = [];
    await context.addInitScript(() => {
        sessionStorage.setItem('backprop.oauth.session', JSON.stringify({
            accessToken: 'browser-fixture-token', expiresAt: Date.now() + 3600000,
            scopes: ['api:use-streams-read', 'api:use-streams-write'],
        }));
    });
    await context.route(url => url.origin === origin && url.pathname === '/', async route => {
        const response = await route.fetch();
        const headers = response.headers();
        headers['content-security-policy'] = headers['content-security-policy']
            .replace("connect-src 'self' blob:", `connect-src 'self' blob: ${host}`);
        await route.fulfill({response, headers});
    });
    await context.route(`${origin}/config.json`, route => route.fulfill({json: config}));
    await context.route(`${host}/**`, async route => {
        const request = route.request();
        const headers = {'Access-Control-Allow-Origin': origin,
            'Access-Control-Allow-Headers': 'authorization,content-type',
            'Access-Control-Allow-Methods': 'GET,POST,OPTIONS'};
        if (request.method() === 'OPTIONS') {
            await route.fulfill({status: 204, headers});
            return;
        }
        requests.push(request.headers());
        const url = new URL(request.url());
        if (url.origin !== host) throw new Error('The test proxy rejected a foreign credential destination');
        let data = request.postDataBuffer() || undefined;
        if (token && project && url.pathname.endsWith('/sqlQueries/execute')) {
            const body = JSON.parse(data.toString());
            const predicate = "project = decode(unhex('" + Buffer.from(project).toString('hex') + "'), 'UTF-8')";
            body.query = body.query.replace(' ORDER BY ', ` WHERE ${predicate} ORDER BY `);
            data = Buffer.from(JSON.stringify(body));
        }
        if (token && initialOffsets && url.pathname.endsWith('/getRecords') && !url.searchParams.has('startOffset')) {
            const partition = url.searchParams.get('partitionId');
            if (initialOffsets[partition] !== undefined) url.searchParams.set('startOffset', initialOffsets[partition]);
        }
        const target = token ? url.href : origin + url.pathname + url.search;
        const response = await context.request.fetch(target, {
            method: request.method(), data,
            maxRedirects: 0,
            headers: {'Content-Type': request.headers()['content-type'] || 'application/json',
                ...(token ? {Authorization: `Bearer ${token}`} : {})},
        });
        await route.fulfill({response, headers: {...response.headers(), ...headers}});
    });
    return requests;
}
