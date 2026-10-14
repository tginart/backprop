# Security

Security fixes target the latest Backprop release.

Report vulnerabilities privately through [GitHub vulnerability
reporting](https://github.com/tginart/backprop/security/advisories/new). If that route
is unavailable, contact a maintainer through their public GitHub profile and request a
private channel before sending details. Do not put credentials, private experiment data
or exploit details in a public issue.

Backprop does not scrub secrets deliberately logged by users. Foundry dataset
permissions determine access; logical project names do not isolate users. Logged HTML is
sandboxed; custom charts reject external resources and use an expression interpreter.
Keep browser assets, deployment CSP and notices together. See [dependency
security](docs/dependency-security.md) for renderer protections and known dependency
risks.
