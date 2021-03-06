
import datetime
import functools
import logging
import time

import flask
import simplejson as json

import checksums
import storage
import toolkit

from .app import app
from .app import cfg
from storage.local import LocalStorage


store = storage.load()
logger = logging.getLogger(__name__)


def require_completion(f):
    """This make sure that the image push correctly finished."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if store.exists(store.image_mark_path(kwargs['image_id'])):
            return toolkit.api_error('Image is being uploaded, retry later')
        return f(*args, **kwargs)
    return wrapper


def set_cache_headers(f):
    """Returns HTTP headers suitable for caching."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        # Set TTL to 1 year by default
        ttl = 31536000
        expires = datetime.datetime.fromtimestamp(int(time.time()) + ttl)
        expires = expires.strftime('%a, %d %b %Y %H:%M:%S GMT')
        headers = {
            'Cache-Control': 'public, max-age={0}'.format(ttl),
            'Expires': expires,
            'Last-Modified': 'Thu, 01 Jan 1970 00:00:00 GMT',
        }
        if 'If-Modified-Since' in flask.request.headers:
            return flask.Response(status=304, headers=headers)
        kwargs['headers'] = headers
        # Prevent the Cookie to be sent when the object is cacheable
        flask.session.modified = False
        return f(*args, **kwargs)
    return wrapper


@app.route('/v1/images/<image_id>/layer', methods=['GET'])
@toolkit.requires_auth
@require_completion
@set_cache_headers
def get_image_layer(image_id, headers):
    try:
        accel_uri_prefix = cfg.nginx_x_accel_redirect
        path = store.image_layer_path(image_id)

        if accel_uri_prefix:
            if isinstance(store, LocalStorage):
                accel_uri = '/'.join([accel_uri_prefix, path])
                headers['X-Accel-Redirect'] = accel_uri

                logger.debug('send accelerated {} ({})'.format(
                    accel_uri, headers))

                return flask.Response('', headers=headers)
            else:
                logger.warn('nginx_x_accel_redirect config set,'
                            ' but storage is not LocalStorage')
        return flask.Response(store.stream_read(path), headers=headers)
    except IOError:
        return toolkit.api_error('Image not found', 404)


@app.route('/v1/images/<image_id>/layer', methods=['PUT'])
@toolkit.requires_auth
def put_image_layer(image_id):
    try:
        json_data = store.get_content(store.image_json_path(image_id))
    except IOError:
        return toolkit.api_error('Image not found', 404)
    layer_path = store.image_layer_path(image_id)
    mark_path = store.image_mark_path(image_id)
    if store.exists(layer_path) and not store.exists(mark_path):
        return toolkit.api_error('Image already exists', 409)
    input_stream = flask.request.stream
    if flask.request.headers.get('transfer-encoding') == 'chunked':
        # Careful, might work only with WSGI servers supporting chunked
        # encoding (Gunicorn)
        input_stream = flask.request.environ['wsgi.input']
    # compute checksums
    csums = []
    sr = toolkit.SocketReader(input_stream)
    tmp, store_hndlr = storage.temp_store_handler()
    sr.add_handler(store_hndlr)
    h, sum_hndlr = checksums.simple_checksum_handler(json_data)
    sr.add_handler(sum_hndlr)
    store.stream_write(layer_path, sr)
    csums.append('sha256:{0}'.format(h.hexdigest()))
    try:
        tmp.seek(0)
        csums.append(checksums.compute_tarsum(tmp, json_data))
        tmp.close()
    except (IOError, checksums.TarError) as e:
        logger.debug('put_image_layer: Error when computing tarsum '
                     '{0}'.format(e))
    try:
        checksum = store.get_content(store.image_checksum_path(image_id))
    except IOError:
        # We don't have a checksum stored yet, that's fine skipping the check.
        # Not removing the mark though, image is not downloadable yet.
        flask.session['checksum'] = csums
        return toolkit.response()
    # We check if the checksums provided matches one the one we computed
    if checksum not in csums:
        logger.debug('put_image_layer: Wrong checksum')
        return toolkit.api_error('Checksum mismatch, ignoring the layer')
    # Checksum is ok, we remove the marker
    store.remove(mark_path)
    return toolkit.response()


@app.route('/v1/images/<image_id>/checksum', methods=['PUT'])
@toolkit.requires_auth
def put_image_checksum(image_id):
    checksum = flask.request.headers.get('X-Docker-Checksum')
    if not checksum:
        return toolkit.api_error('Missing Image\'s checksum')
    if not flask.session.get('checksum'):
        return toolkit.api_error('Checksum not found in Cookie')
    if not store.exists(store.image_json_path(image_id)):
        return toolkit.api_error('Image not found', 404)
    mark_path = store.image_mark_path(image_id)
    if not store.exists(mark_path):
        return toolkit.api_error('Cannot set this image checksum', 409)
    err = store_checksum(image_id, checksum)
    if err:
        return toolkit.api_error(err)
    if checksum not in flask.session.get('checksum', []):
        logger.debug('put_image_layer: Wrong checksum')
        return toolkit.api_error('Checksum mismatch')
    # Checksum is ok, we remove the marker
    store.remove(mark_path)
    return toolkit.response()


@app.route('/v1/images/<image_id>/json', methods=['GET'])
@toolkit.requires_auth
@require_completion
@set_cache_headers
def get_image_json(image_id, headers):
    try:
        data = store.get_content(store.image_json_path(image_id))
    except IOError:
        return toolkit.api_error('Image not found', 404)
    try:
        size = store.get_size(store.image_layer_path(image_id))
        headers['X-Docker-Size'] = str(size)
    except OSError:
        pass
    checksum_path = store.image_checksum_path(image_id)
    if store.exists(checksum_path):
        headers['X-Docker-Checksum'] = store.get_content(checksum_path)
    return toolkit.response(data, headers=headers, raw=True)


@app.route('/v1/images/<image_id>/ancestry', methods=['GET'])
@toolkit.requires_auth
@require_completion
@set_cache_headers
def get_image_ancestry(image_id, headers):
    try:
        data = store.get_content(store.image_ancestry_path(image_id))
    except IOError:
        return toolkit.api_error('Image not found', 404)
    return toolkit.response(json.loads(data), headers=headers)


def generate_ancestry(image_id, parent_id=None):
    if not parent_id:
        store.put_content(store.image_ancestry_path(image_id),
                          json.dumps([image_id]))
        return
    data = store.get_content(store.image_ancestry_path(parent_id))
    data = json.loads(data)
    data.insert(0, image_id)
    store.put_content(store.image_ancestry_path(image_id), json.dumps(data))


def check_images_list(image_id):
    full_repos_name = flask.session.get('repository')
    if not full_repos_name:
        # We only enforce this check when there is a repos name in the session
        # otherwise it means that the auth is disabled.
        return True
    try:
        path = store.images_list_path(*full_repos_name.split('/'))
        images_list = json.loads(store.get_content(path))
    except IOError:
        return False
    return (image_id in images_list)


def store_checksum(image_id, checksum):
    checksum_parts = checksum.split(':')
    if len(checksum_parts) != 2:
        return 'Invalid checksum format'
    # We store the checksum
    checksum_path = store.image_checksum_path(image_id)
    store.put_content(checksum_path, checksum)


@app.route('/v1/images/<image_id>/json', methods=['PUT'])
@toolkit.requires_auth
def put_image_json(image_id):
    try:
        data = json.loads(flask.request.data)
    except json.JSONDecodeError:
        pass
    if not data or not isinstance(data, dict):
        return toolkit.api_error('Invalid JSON')
    if 'id' not in data:
        return toolkit.api_error('Missing key `id\' in JSON')
    # Read the checksum
    checksum = flask.request.headers.get('X-Docker-Checksum')
    if checksum:
        # Storing the checksum is optional at this stage
        err = store_checksum(image_id, checksum)
        if err:
            return toolkit.api_error(err)
    else:
        # We cleanup any old checksum in case it's a retry after a fail
        store.remove(store.image_checksum_path(image_id))
    if image_id != data['id']:
        return toolkit.api_error('JSON data contains invalid id')
    if check_images_list(image_id) is False:
        return toolkit.api_error('This image does not belong to the '
                                 'repository')
    parent_id = data.get('parent')
    if parent_id and not store.exists(store.image_json_path(data['parent'])):
        return toolkit.api_error('Image depends on a non existing parent')
    json_path = store.image_json_path(image_id)
    mark_path = store.image_mark_path(image_id)
    if store.exists(json_path) and not store.exists(mark_path):
        return toolkit.api_error('Image already exists', 409)
    # If we reach that point, it means that this is a new image or a retry
    # on a failed push
    store.put_content(mark_path, 'true')
    store.put_content(json_path, flask.request.data)
    generate_ancestry(image_id, parent_id)
    return toolkit.response()
