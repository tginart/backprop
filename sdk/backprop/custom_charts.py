"""Foundry-stored chart definitions with explicit field substitution.

Architecture: Api registers a JSON specification -> plot_table resolves its ID
through the active run -> chart event embeds its specification and Table -> the
browser renders locally. No W&B hosted chart lookup or external code execution.
Main callers: Api.create_custom_chart and plot.plot_table.
"""
from backprop.errors import UnsupportedFeatureError

import json

from .event_order import ordered_events


def register(api, entity, name, display_name, spec_type, access, spec):
    if spec_type != 'vega2': raise ValueError("spec_type must be vega2")
    if access not in {'private', 'public'}: raise ValueError("access must be private or public")
    if access == 'public': raise UnsupportedFeatureError("Foundry dataset permissions cannot publish a chart anonymously")
    if not name or '/' in name or not entity or '/' in entity: raise ValueError("Invalid chart entity/name")
    spec = json.loads(spec) if isinstance(spec, str) else spec
    if not isinstance(spec, dict): raise TypeError("Chart specification must be a JSON object")
    json.dumps(spec, allow_nan=False)
    from .apis.public.mutations import writer_for
    writer_for(api, entity, '__charts__', '__charts__').write('custom_chart', {
        'name': name, 'display_name': display_name, 'spec': spec, 'spec_type': spec_type})
    return f'{entity}/{name}'


def resolve(run, identity):
    if run._sink is None: raise ValueError("Custom chart lookup requires an authenticated store")
    parts = identity.split('/')
    if len(parts) != 2: raise ValueError("Custom chart ID must be entity/name")
    events = ordered_events(run._sink.read_events(entity=parts[0], project='__charts__'))
    matches = [e for e in events if e['type'] == 'custom_chart' and e['payload']['name'] == parts[1]]
    if not matches: raise ValueError(f"Unknown Backprop chart: {identity}; register it with Api.create_custom_chart")
    return matches[-1]['payload']['spec']


def substitute(value, fields, strings):
    """Substitute typed values, never interpolate JSON source or execute text."""
    if isinstance(value, dict): return {k: substitute(v, fields, strings) for k, v in value.items()}
    if isinstance(value, list): return [substitute(v, fields, strings) for v in value]
    if isinstance(value, str):
        for prefix, mapping in [('field', fields), ('string', strings)]:
            token = '${' + prefix + ':'
            if value.startswith(token) and value.endswith('}'):
                key = value[len(token):-1]
                if key not in mapping: raise ValueError(f"Missing chart {prefix}: {key}")
                return mapping[key]
    return value
