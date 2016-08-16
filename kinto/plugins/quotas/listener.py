import copy

from pyramid.httpexceptions import HTTPInsufficientStorage
from kinto.core.errors import http_error, ERRORS
from kinto.core.storage.exceptions import RecordNotFoundError
from kinto.core.utils import instance_uri

from .utils import record_size


def get_bucket_settings(settings, bucket_id, name):
    return settings.get(
        # Bucket specific
        'quotas.bucket_{}_{}'.format(bucket_id, name),
        # Global to all buckets
        settings.get('quotas.bucket_{}'.format(name), None))


def get_collection_settings(settings, bucket_id, collection_id, name):
    return settings.get(
        # Specific for a given bucket collection
        'quotas.collection_{}_{}_{}'.format(bucket_id, collection_id, name),
        # Specific to given bucket collections
        settings.get('quotas.collection_{}_{}'.format(bucket_id, name),
                     # Global to all buckets collections
                     settings.get('quotas.collection_{}'.format(name), None)))


def on_resource_changed(event):
    """
    Everytime an object is created/changed/deleted, we update the
    bucket counters.

    If a new object exceeds the quotas, we reject the request.
    """
    payload = event.payload
    action = payload['action']
    resource_name = payload['resource_name']
    event_uri = payload['uri']

    settings = event.request.registry.settings

    bucket_id = payload['bucket_id']
    bucket_uri = instance_uri(event.request, 'bucket', id=bucket_id)
    collection_id = None
    collection_uri = None
    if 'collection_id' in payload:
        collection_id = payload['collection_id']
        collection_uri = instance_uri(event.request,
                                      'collection',
                                      bucket_id=bucket_id,
                                      id=collection_id)

    bucket_max_bytes = get_bucket_settings(settings, bucket_id, 'max_bytes')
    bucket_max_items = get_bucket_settings(settings, bucket_id, 'max_items')
    bucket_max_bytes_per_item = get_bucket_settings(settings, bucket_id,
                                                    'max_bytes_per_item')
    collection_max_bytes = get_collection_settings(settings, bucket_id,
                                                   collection_id, 'max_bytes')
    collection_max_items = get_collection_settings(settings, bucket_id,
                                                   collection_id, 'max_items')
    collection_max_bytes_per_item = get_collection_settings(
        settings, bucket_id, collection_id, 'max_bytes_per_item')

    # XXX: Maybe we want to differenciate between
    #      bucket/collection/records/group payload.
    max_bytes_per_item = (collection_max_bytes_per_item or
                          bucket_max_bytes_per_item)

    storage = event.request.registry.storage

    if action == 'delete' and resource_name == 'bucket':
        try:
            storage.delete(parent_id=bucket_uri,
                           collection_id='quota',
                           object_id='bucket_info')
        except RecordNotFoundError:
            pass

        collection_pattern = instance_uri(event.request, 'collection',
                                          bucket_id=bucket_id, id='*')
        storage.delete_all(parent_id=collection_pattern,
                           collection_id='quota')
        return

    targets = []
    for impacted in event.impacted_records:
        target = impacted['new' if action != 'delete' else 'old']
        # On POST .../records, the URI does not contain the newly created
        # record id.
        obj_id = target['id']
        parts = event_uri.split('/')
        if resource_name in parts[-1]:
            parts.append(obj_id)
        else:
            # Make sure the id is correct on grouped events.
            parts[-1] = obj_id
        uri = '/'.join(parts)

        old = impacted.get('old', {})
        new = impacted.get('new', {})

        targets.append((uri, obj_id, old, new))

    try:
        bucket_info = copy.deepcopy(
            storage.get(parent_id=bucket_uri,
                        collection_id='quota',
                        object_id='bucket_info'))
    except RecordNotFoundError:
        bucket_info = {
            "collection_count": 0,
            "record_count": 0,
            "storage_size": 0,
        }

    collection_info = {
        "record_count": 0,
        "storage_size": 0,
    }
    if collection_id:
        try:
            collection_info = copy.deepcopy(
                storage.get(parent_id=collection_uri,
                            collection_id='quota',
                            object_id='collection_info'))
        except RecordNotFoundError:
            pass

    # Update the bucket quotas values for each impacted record.
    for (uri, obj_id, old, new) in targets:
        if resource_name == "collection":
            collection_uri = uri
        old_size = record_size(old)
        new_size = record_size(new)

        if max_bytes_per_item is not None and action != "delete":
            if new_size > max_bytes_per_item:
                message = ("MAX_BYTES_PER_ITEM size exceeded: %d Bytes. "
                           "MAX_BYTES_PER_ITEM is %d Bytes" % (
                               new_size, max_bytes_per_item))
                raise http_error(HTTPInsufficientStorage(),
                                 errno=ERRORS.FORBIDDEN.value,
                                 message=message)

        if action == 'create':
            bucket_info['storage_size'] += new_size
            if resource_name == 'collection':
                bucket_info['collection_count'] += 1
                collection_info['storage_size'] += new_size
            if resource_name == 'record':
                bucket_info['record_count'] += 1
                collection_info['record_count'] += 1
                collection_info['storage_size'] += new_size
        elif action == 'update':
            bucket_info['storage_size'] -= old_size
            bucket_info['storage_size'] += new_size
            if resource_name in ('collection', 'record'):
                collection_info['storage_size'] -= old_size
                collection_info['storage_size'] += new_size
        elif action == 'delete':
            bucket_info['storage_size'] -= old_size
            if resource_name == 'collection':
                bucket_info['collection_count'] -= 1
                # When we delete the collection all the records in it
                # are deleted without notification.
                collection_records, _ = storage.get_all(
                    collection_id='record',
                    parent_id=collection_uri)
                for r in collection_records:
                    old_record_size = record_size(r)
                    bucket_info['record_count'] -= 1
                    bucket_info['storage_size'] -= old_record_size
                    collection_info['record_count'] -= 1
                    collection_info['storage_size'] -= old_record_size
                collection_info['storage_size'] -= old_size

            if resource_name == 'record':
                bucket_info['record_count'] -= 1
                collection_info['record_count'] -= 1
                collection_info['storage_size'] -= old_size

    if bucket_max_bytes is not None:
        if bucket_info['storage_size'] > bucket_max_bytes:
            message = ("Bucket size exceeded: %d Bytes. "
                       "MAX_BYTES is %d Bytes" % (
                           bucket_info['storage_size'], bucket_max_bytes))
            raise http_error(HTTPInsufficientStorage(),
                             errno=ERRORS.FORBIDDEN.value,
                             message=message)

    if bucket_max_items is not None:
        if bucket_info['record_count'] > bucket_max_items:
            message = ("Bucket max items exceeded: %d records. "
                       "MAX_ITEMS is %d records" % (
                           bucket_info['record_count'], bucket_max_items))
            raise http_error(HTTPInsufficientStorage(),
                             errno=ERRORS.FORBIDDEN.value,
                             message=message)

    if collection_max_bytes is not None:
        if collection_info['storage_size'] > collection_max_bytes:
            message = ("Collection size exceeded: %d Bytes. "
                       "MAX_BYTES is %d Bytes" % (
                           collection_info['storage_size'],
                           collection_max_bytes))
            raise http_error(HTTPInsufficientStorage(),
                             errno=ERRORS.FORBIDDEN.value,
                             message=message)

    if collection_max_items is not None:
        if collection_info['record_count'] > collection_max_items:
            message = ("Collection max items exceeded: %d records. "
                       "MAX_ITEMS is %d records" % (
                           collection_info['record_count'],
                           collection_max_items))
            raise http_error(HTTPInsufficientStorage(),
                             errno=ERRORS.FORBIDDEN.value,
                             message=message)

    storage.update(parent_id=bucket_uri,
                   collection_id='quota',
                   object_id='bucket_info',
                   record=bucket_info)

    if collection_id:
        if action == 'delete' and resource_name == 'collection':
            try:
                storage.delete(parent_id=collection_uri,
                               collection_id='quota',
                               object_id='collection_info')
            except RecordNotFoundError:
                pass
            return
        else:
            storage.update(parent_id=collection_uri,
                           collection_id='quota',
                           object_id='collection_info',
                           record=collection_info)
