"""Public Image files and annotations over the existing media/artifact paths.

Architecture: generated pixels freeze into an owned file -> explicit binding
uses Media's copy/move/link rules -> public descriptors refer to those files.
Annotation objects keep their own file lifecycle and class-label publication.
Native Encoder still writes the existing Foundry/browser representation.
Main callers: Image public methods. No new storage or uploader is introduced.
"""
from __future__ import annotations
import hashlib
import logging
import os
from .media_base import Media, _filename


def layers(image, family):
    """Reuse original annotation objects or rebuild a saved native descriptor.

    Main callers: binding, public serializers and batch helpers. Native artifact
    reads restore inline metadata, so reconstruct its public helper objects once
    while retaining the same pixels, labels and ordering.
    """
    cache = image._public_annotations
    if family not in cache:
        from .media import BoundingBoxes2D, ImageMask
        from .table_image_type import _numeric_labels
        values = {}
        for key, value in image.metadata.get(family, {}).items():
            if family == 'boxes':
                values[key] = BoundingBoxes2D({'box_data': value['box_data'],
                    'class_labels': _numeric_labels(value.get('class_labels', {}))}, key)
            else:
                mask = value['mask']
                values[key] = ImageMask({'path': mask._path,
                    'class_labels': _numeric_labels(value.get('class_labels', {}))}, key)
        cache[family] = values
    return cache[family]


def bind(image, run, key, step, id_, ignore_copy_err):
    """Bind pixels when needed, followed by box and mask files in source order.

    Main caller: Image.bind_to_run. Online source-artifact images need no pixel
    copy, but annotations still bind. Offline runs always copy pixels. The
    evolving annotation ID follows the pinned naming rule across both families.
    """
    if image.path_is_reference(image._path):
        raise ValueError('Image media created by a reference to external storage cannot currently be added to a run')
    _filename(key, step, '', '')  # Validate Windows keys before owner lookup.
    if run.offline or image._get_artifact_entry_ref_url() is None:
        Media.bind_to_run(image, run, key, step, id_, ignore_copy_err)
    for family in ['boxes', 'masks']:
        for index, value in enumerate(layers(image, family).values()):
            id_ = str(id_) + str(index) if id_ is not None else None
            value.bind_to_run(run, key, step, id_, ignore_copy_err=ignore_copy_err)


def serialize(image, owner):
    """Return the owner-specific public descriptor and annotation dependencies.

    Main caller: Image.to_json. Artifact contents remain immutable; class files
    use the existing artifact object codec. This is separate from native history
    encoding, which preserves labels inline for the Foundry browser.
    """
    from .media import _FileEncodedMedia
    from .run import Run
    from .artifacts import Artifact
    result = _FileEncodedMedia.to_json(image, owner)
    result.update(_type='image-file', format=image.format)
    if not hasattr(image, '_reference_uri') and (image.metadata.get('width') is None or image.metadata.get('height') is None):
        image.metadata['width'], image.metadata['height'] = image.image.size
    for field in ['width', 'height']:
        if image.metadata.get(field) is not None: result[field] = image.metadata[field]
    if image.metadata.get('grouping'): result['grouping'] = image.metadata['grouping']
    if isinstance(owner, Artifact):
        classes = image.metadata.get('classes')
        if (layers(image, 'boxes') or layers(image, 'masks')) and classes is None:
            raise ValueError('classes must be passed to wandb.Image which have masks or bounding boxes when adding to artifacts')
        if classes is not None:
            name = 'media/classes/' + hashlib.md5(str(classes._class_set).encode()).hexdigest() + '_cls'
            entry = owner.add(classes, name)
            result['classes'] = {'type': 'classes-file', 'path': entry.path, 'digest': entry.digest}
    elif not isinstance(owner, Run):
        raise TypeError('to_json accepts wandb.Run or wandb_artifact.Artifact')
    for family in ['boxes', 'masks']:
        values = layers(image, family)
        if values: result[family] = {key: value.to_json(owner) for key, value in values.items()}
    return result


def deserialize(cls, json_obj, source_artifact):
    """Read declared class, annotation and pixel files through the source owner.

    Main caller: Image.from_json. Public descriptors use helper-specific files;
    historical native descriptors retain their existing decoder. Neither form
    imports classes or executes Python from saved data.
    """
    from .media import Classes, ImageMask, BoundingBoxes2D
    if isinstance(json_obj, dict) and json_obj.get('_type') == 'image':
        from .object_codec import decode_object
        return decode_object(json_obj, source_artifact)
    classes = None
    if json_obj.get('classes') is not None:
        classes = source_artifact.get(json_obj['classes']['path'])
        assert isinstance(classes, (type(None), Classes))
    groups = {}
    for family, kind in [('masks', ImageMask), ('boxes', BoundingBoxes2D)]:
        values = json_obj.get(family)
        groups[family] = None
        if values:
            groups[family] = {}
            for key, descriptor in values.items():
                item = kind.from_json(descriptor, source_artifact)
                if family == 'masks': item._set_artifact_source(source_artifact)
                item._key = key
                groups[family][key] = item
    return cls(source_artifact.get_entry(json_obj['path']).download(),
        caption=json_obj.get('caption'), grouping=json_obj.get('grouping'),
        classes=classes, **groups)


def collect(images, run, family):
    """Collect annotation descriptors while retaining unannotated positions.

    Main callers: Image.all_masks/all_boxes. Every existing helper serializes
    under the supplied owner; invalid/unbound annotations keep their own errors.
    """
    groups = []
    for image in images:
        values = layers(image, family)
        groups.append({key: value.to_json(run) for key, value in values.items()} if values else None)
    return groups if groups and any(value is not None for value in groups) else False


def batch(cls, seq, run, key, step):
    """Build the public separated-image descriptor from bound image objects.

    Main caller: Image.seq_to_json. Preserve pinned empty/generator errors,
    directory validation, mixed-size warning and artifact-reference filenames.
    """
    from .media import Image
    descriptors = [value.to_json(run) for value in seq]
    for value in descriptors:
        if 'path' in value and not value['path'].startswith(cls.get_media_subdir()):
            raise ValueError("Files in an array of Image's must be in the {} directory, not {}".format(cls.get_media_subdir(), value['path']))
    count = len(seq)
    width, height = seq[0].image.size
    if not all(image.image.size == (width, height) for image in seq):
        logging.warning('Images sizes do not match. This will causes images to be display incorrectly in the UI.')
    result = {'_type': 'images/separated', 'width': width, 'height': height,
        'format': descriptors[0]['format'], 'count': count,
        'filenames': [value.get('path', value.get('artifact_path')) for value in descriptors]}
    for name, value in [('captions', Image.all_captions(seq)),
            ('all_masks', Image.all_masks(seq, run, key, step)),
            ('all_boxes', Image.all_boxes(seq, run, key, step))]:
        if value: result[name] = value
    return result
