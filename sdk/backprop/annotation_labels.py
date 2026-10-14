"""Merge one annotation layer without replacing another writer's labels.

Architecture: existing config event with annotation_labels -> validate -> copy
the reserved map -> replace only the named layer. Main callers: Run binding,
restart/branch replay and Public Api reduction. Event order resolves same-layer
conflicts; distinct layers converge without a mutable server-side registry.
"""
import copy


def apply_annotation_update(config,entry):
    """Apply one complete label map atomically while retaining sibling layers.

    Main callers: _add_singleton and the three existing replay boundaries.
    Whole user config assignments still replace their keys normally; only
    annotation-generated updates use this narrower scope.
    """
    if (not isinstance(entry,dict) or entry.get('type') not in {'mask/class_labels','bounding_box/class_labels'}
            or not isinstance(entry.get('key'),str) or not isinstance(entry.get('value'),dict)
            or any(not isinstance(v,str) for v in entry['value'].values())):
        raise ValueError('Invalid annotation label update')
    internal=copy.deepcopy(config.get('_wandb',{}))
    if not isinstance(internal,dict):raise ValueError('Annotation metadata requires a mapping')
    group=internal.setdefault(entry['type'],{})
    if not isinstance(group,dict):raise ValueError('Annotation layers require a mapping')
    group[entry['key']]=copy.deepcopy(entry)
    config['_wandb']=internal
