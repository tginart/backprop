"""Real collection mutations -> durable events -> shared Python/browser fixture.

Main callers: collection contract tests. File hashes, versions and membership
stay immutable while two stale editors change independent collection fields.
"""
import copy
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import backprop
from backprop.apis.public import Api


def fixture():
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'BACKPROP_DIR': directory}):
        store = backprop.MemorySink()
        api = Api._from_store(store, entity='team', project='collections')
        settings = {'silent': True, 'console': 'off', 'disable_git': True}
        with backprop.Run(entity='team', project='collections', id='producer', dir=directory,
                          _sink=store, settings=settings) as run:
            artifacts = []
            for index in range(2):
                artifact = backprop.Artifact('weights', 'model')
                with artifact.new_file('weights.txt') as file: file.write(str(index))
                artifacts.append(run.log_artifact(artifact, aliases=[f'epoch{index}']))
        original = copy.deepcopy([e for e in store.events if e['type'] == 'artifact'])
        consumer = api.create_run(run_id='consumer')
        consumer.use_artifact(api.artifact(artifacts[0].id))
        one = api.artifact_collection('model', 'weights')
        stale = api.artifact_collection('model', 'weights')
        identity = one.id
        one.name, one.type = 'trained-weights', 'checkpoint'
        one.save()
        stale.description, stale.tags = 'reviewed weights', ['approved']
        stale.save()
        # A new version joins the renamed collection; an old handle still edits
        # its immutable member rather than recreating the former name.
        old = api.artifact(artifacts[0].id)
        old.aliases.append('best'); old.save()
        with backprop.Run(entity='team', project='collections', id='later', dir=directory,
                          _sink=store, settings=settings) as run:
            artifact = backprop.Artifact('trained-weights', 'checkpoint')
            with artifact.new_file('weights.txt') as file: file.write('later')
            run.log_artifact(artifact)
        current = api.artifact_collection('checkpoint', 'trained-weights')
        assert current.id == identity
        assert [e for e in store.events if e['type'] == 'artifact'][:2] == original
        assert api.artifact('trained-weights:epoch0').version == artifacts[0].version
        assert api.artifact('trained-weights:best').get_entry('weights.txt')._read_verified_bytes() == b'0'
        assert list(consumer.used_artifacts())[0].name.startswith('trained-weights:')
        payloads = api._artifact_events('team', 'collections')
        return dict(events=store.events, collection=dict(id=current.id, name=current.name,
            artifact_type=current.type, description=current.description, tags=current.tags),
            expected=[dict(run_id=e['run_id'], artifact_id=e['payload']['artifact_id'],
                name=e['payload']['name'], artifact_type=e['payload']['artifact_type'],
                version=e['payload']['version'], aliases=e['payload']['aliases']) for e in payloads])


if __name__ == '__main__':
    print(json.dumps(fixture()))
