"""Run actual public collection save methods with only the service substituted.

Main caller: collection contract test. Reference data uses W&B's real validated
model and mutation builders; native data comes from real Backprop publication.
Original exceptions remain in the output rather than being normalized away.
"""
import json
import os
import sys
import tempfile
from types import SimpleNamespace

if sys.argv[1] == 'native':
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()
import wandb


def observe():
    with tempfile.TemporaryDirectory() as directory:
        os.environ['BACKPROP_DIR'] = directory
        if sys.argv[1] == 'reference':
            assert wandb.__version__ == '0.30.0'
            from wandb.apis.public import ArtifactCollection
            from wandb.sdk.artifacts._models.artifact_collection import ArtifactCollectionData
            value = ArtifactCollection.__new__(ArtifactCollection)
            value._current = ArtifactCollectionData(__typename='ArtifactSequence', id='opaque',
                name='original', type='model', entity='e', project='p', project_id='project',
                created_at='2026-01-01T00:00:00Z')
            value._saved = value._current.model_copy(deep=True)
            value._service_api = SimpleNamespace(execute_graphql=lambda *args, **kwargs: {})
        else:
            from backprop.apis.public import Api
            sink = wandb.MemorySink()
            with wandb.Run(entity='e', project='p', id='r', dir=directory, _sink=sink,
                           settings={'silent': True, 'console': 'off'}) as run:
                run.log_artifact(wandb.Artifact('original', 'model'))
            value = Api._from_store(sink, entity='e', project='p').artifact_collection('model', 'original')
        identity = value.id
        outputs = []
        for changes in ({}, {'description': 'reviewed', 'tags': ['a','a','b']},
                        {'name': 'renamed', 'type': 'dataset'}, {'tags': 'single tag'},
                        {'description': None, 'tags': []}):
            for field, item in changes.items(): setattr(value, field, item)
            result = value.save()
            outputs.append(dict(result=result, name=value.name, type=value.type,
                description=value.description, tags=value.tags, stable_id=value.id == identity))
        failures = []
        for field, item in [('entity','other'), ('project','other'), ('name','bad/name'),
                            ('name','a'*129), ('tags',['bad/tag']), ('type','job')]:
            try:
                setattr(value,field,item)
                value.save()
            except Exception as error:
                failures.append(dict(field=field, value=item, type=type(error).__name__, message=str(error)))
            else:
                failures.append(dict(field=field, value=item, type=None, message=None))
        return dict(outputs=outputs, failures=failures)


if __name__ == '__main__': print(json.dumps(observe()))
