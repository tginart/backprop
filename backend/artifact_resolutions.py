"""Store one immutable incremental-artifact resolution in existing file storage.

Architecture: hashed artifact identity -> read a fixed JSON file -> APPEND the
first candidate -> read the winner if another writer already created it.
Foundry commits the implicit transaction and aborts a rejected duplicate. No
client-held transaction, scheduler, dataset, branch or hosted service is added.
Main callers: FoundryStore's incremental resolution methods.
"""
import json
import re
from urllib.parse import quote,urlencode


def _endpoint(store,key):
    if not isinstance(key,str) or re.fullmatch(r'[0-9a-f]{64}',key) is None:
        raise ValueError('Artifact resolution key must be a SHA-256 identity')
    dataset=quote(store.config['media_dataset_rid'],safe='')
    path=quote('artifact-resolutions/'+key+'.json',safe='')
    return '/api/v2/datasets/'+dataset+'/files/'+path


def read(store,key):
    """Return a saved choice, distinguishing an absent file from denied access.

    Main callers: prepare and claim. Only FileNotFoundOnBranch means absent;
    missing datasets/branches, permission failures and malformed JSON propagate.
    """
    from .http_store import BackendError as FoundryError
    path=_endpoint(store,key)+'/content?'+urlencode({'branchName':store.branch})
    try:
        data=store.request(path,retry=True,raw=True)
    except FoundryError as error:
        if error.status_code==404 and error.error_name=='FileNotFoundOnBranch':
            return None
        raise
    value=json.loads(data)
    if not isinstance(value,dict):
        raise ValueError('Artifact resolution file must contain a JSON object')
    return value


def claim(store,key,candidate):
    """Atomically keep the first completed manifest, including ambiguous retries.

    Main caller: deferred_incremental_artifacts before publishing stream events.
    APPEND cannot overwrite an existing path. The same request can therefore be
    retried after a lost response; FileAlreadyExists leads to an authenticated
    read of the committed winner, never to an overwrite or a guessed success.
    """
    from .http_store import BackendError as FoundryError
    path=_endpoint(store,key)+'/upload?'+urlencode({'branchName':store.branch,'transactionType':'APPEND'})
    data=json.dumps(candidate,allow_nan=False,separators=(',',':')).encode()
    try:
        store.request(path,'POST',data,'application/octet-stream',retry=True)
    except FoundryError as error:
        if error.status_code==404 and error.error_name=='FileAlreadyExists':
            winner=read(store,key)
            if winner is None:
                raise store.error_type('Committed artifact resolution is not yet readable; retry without replacing it',retryable=True) from error
            return winner
        raise
    return json.loads(data)
