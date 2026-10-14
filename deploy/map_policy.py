"""Choose the public map providers allowed by a Backprop website release.

Architecture: operator origin list -> strict HTTPS validation -> browser config
and matching content-security policy. Finite figure resources are saved in
Foundry; dynamic map tiles, glyphs and sprites use these external providers.
No credential or private operator setting belongs in this module's output.
Main callers: deploy.public_config and deploy.apply_content_security_policy.
"""

import re
from urllib.parse import urlsplit


DEFAULT_MAP_RESOURCE_ORIGINS = (
    "https://tile.openstreetmap.org",
    "https://a.tile.openstreetmap.org",
    "https://b.tile.openstreetmap.org",
    "https://fonts.openmaptiles.org",
    "https://basemaps.cartocdn.com",
    "https://tiles.basemaps.cartocdn.com",
    "https://tiles-a.basemaps.cartocdn.com",
    "https://tiles-b.basemaps.cartocdn.com",
    "https://tiles-c.basemaps.cartocdn.com",
    "https://tiles-d.basemaps.cartocdn.com",
    "https://cartodb-basemaps-c.global.ssl.fastly.net",
    "https://server.arcgisonline.com",
    "https://tilemaps.icgc.cat",
    "https://geoserveis.icgc.cat",
    "https://api.mapbox.com",
    "https://tiles.stadiamaps.com",
)
# Plotly's pinned MapLibre renderer requests only validated icon identifiers at
# this exact versioned directory. Keep the exception narrower than a CDN origin.
MAKI_ICON_SOURCE = "https://cdn.jsdelivr.net/npm/@mapbox/maki@8.2.0/icons/"
_HOST = re.compile(r"(?=.{1,253}\Z)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\Z")
_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


def map_resource_origins(state):
    """Validate and normalize the deployment's explicit provider permissions.

    Main callers: public_config. An absent setting uses the pinned renderer's
    standard providers; an explicit empty list permits no external map provider.
    Invalid values fail the build instead of silently widening permissions.
    Only origins are accepted so values cannot inject additional CSP directives.
    """
    values = state.get("map_resource_origins", list(DEFAULT_MAP_RESOURCE_ORIGINS))
    if not isinstance(values, list):
        raise ValueError("map_resource_origins must be a list of HTTPS origins")
    result = []
    for value in values:
        try:
            if not isinstance(value, str) or any(character.isspace() for character in value):
                raise ValueError
            parsed = urlsplit(value)
            hostname, port = parsed.hostname, parsed.port
            if (parsed.scheme != "https" or not hostname or not _HOST.fullmatch(hostname)
                    or any(not _LABEL.fullmatch(label) for label in hostname.split("."))
                    or parsed.username is not None or parsed.password is not None
                    or parsed.path or parsed.query or parsed.fragment
                    or any(character in value for character in ("?", "#", "\\"))
                    or parsed.netloc.endswith(":")):
                raise ValueError
            origin = "https://" + hostname.lower()
            if port is not None and port != 443:
                if port < 1:
                    raise ValueError
                origin += ":" + str(port)
        except (ValueError, TypeError):
            raise ValueError("map_resource_origins accepts only HTTPS origins without paths, credentials or wildcards") from None
        if origin not in result:
            result.append(origin)
    return result
