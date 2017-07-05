#!/usr/bin/env python3
import json
import re
import sys
import _thread as thread
import threading
import unittest
from datetime import datetime, timedelta, timezone

import docker
import iso8601
import requests
from testtools.assertions import assert_that
from testtools.matchers import (
    AfterPreprocessing as After, Contains, Equals, GreaterThan, Is, LessThan,
    MatchesAll, MatchesAny, MatchesDict, MatchesRegex, Not)

POSTGRES_IMAGE = 'postgres:9.6-alpine'
POSTGRES_PARAMS = {
    'service': 'db',
    'db': 'mysite',
    'user': 'mysite',
    'password': 'secret',
}
RABBITMQ_IMAGE = 'rabbitmq:3.6-alpine'
RABBITMQ_PARAMS = {
    'service': 'amqp',
    'vhost': '/mysite',
    'user': 'mysite',
    'password': 'secret',
}
DJANGO_BOOTSTRAP_IMAGE = 'mysite:py3'
DATABASE_URL = (
    'postgres://{user}:{password}@{service}/{db}'.format(**POSTGRES_PARAMS))
BROKER_URL = (
    'amqp://{user}:{password}@{service}/{vhost}'.format(**RABBITMQ_PARAMS))


def resource_name(name, namespace='test'):
    return '{}_{}'.format(namespace, name)


def quit_function(fn_name):
    # https://stackoverflow.com/a/31667005
    print('{} took too long'.format(fn_name), file=sys.stderr)
    sys.stderr.flush()  # Python 3 stderr is likely buffered.
    # FIXME: Interrupting the main thread is hacky
    thread.interrupt_main()  # raises KeyboardInterrupt


def exit_after(s):
    """
    Use as decorator to exit process if function takes longer than s seconds
    https://stackoverflow.com/a/31667005
    """
    def outer(fn):
        def inner(*args, **kwargs):
            timer = threading.Timer(s, quit_function, args=[fn.__name__])
            timer.start()
            try:
                result = fn(*args, **kwargs)
            finally:
                timer.cancel()
            return result
        return inner
    return outer


@exit_after(10)
def wait_for_log_line(container, pattern):
    for line in container.logs(stream=True):
        line = line.decode('utf-8').rstrip()  # Drop the trailing newline
        if re.search(pattern, line):
            return line


class DockerHelper(object):
    def setup(self):
        self._client = docker.client.from_env()
        self._network = self._client.networks.create(
            resource_name('default'), driver='bridge')
        self._containers = {}

    def teardown(self):
        # Remove all containers
        for service in self._containers.keys():
            self.stop_and_remove_container(service)

        # Remove the network
        self._network.remove()

    def create_container(self, name, image, **kwargs):
        container_name = resource_name(name)
        print("Creating container '{}'...".format(container_name))
        container = self._client.containers.create(
            image, name=container_name, detach=True, network=self._network.id,
            **kwargs)

        # FIXME: Hack to make sure the container has the right network aliases.
        # If we don't specify a network when the container is created then the
        # default bridge network is attached which we don't want.
        self._network.disconnect(container)
        self._network.connect(container, aliases=[name])

        self._put_container(name, container)

    def get_container(self, name):
        assert name in self._containers
        return self._containers[name]

    def _put_container(self, name, container):
        assert name not in self._containers
        self._containers[name] = container

    def start_container(self, name, log_line_pattern):
        container = self.get_container(name)
        print("Starting container '{}'...".format(container.name))
        container.start()
        print(wait_for_log_line(container, log_line_pattern))
        container.reload()
        print("Container status: '{}'".format(container.status))
        assert container.status == 'running'
        print()

    def stop_and_remove_container(
            self, name, stop_timeout=5, remove_force=True):
        container = self.get_container(name)
        print("Stopping container '{}'...".format(container.name))
        container.stop(timeout=stop_timeout)
        print("Removing container '{}'...".format(container.name))
        container.remove(force=remove_force)
        print()

    def pull_image_if_not_found(self, image):
        try:
            self._client.images.get(image)
            print("Image '{}' found".format(image))
        except docker.errors.ImageNotFound:
            print("Pulling image '{}'...".format(image))
            self._client.images.pull(image)

    def get_container_host_port(self, name, container_port, index=0):
        # FIXME: Bit of a hack to get the port number on the host
        container = self.get_container(name)
        inspection = self._client.api.inspect_container(container.id)
        return (inspection['NetworkSettings']['Ports']
                [container_port][index]['HostPort'])


docker_helper = DockerHelper()


def setUpModule():
    docker_helper.setup()
    setup_db(docker_helper)
    setup_amqp(docker_helper)


def setup_db(docker_helper):
    docker_helper.pull_image_if_not_found(POSTGRES_IMAGE)

    docker_helper.create_container(
        POSTGRES_PARAMS['service'], POSTGRES_IMAGE, environment={
            'POSTGRES_DB': POSTGRES_PARAMS['db'],
            'POSTGRES_USER': POSTGRES_PARAMS['user'],
            'POSTGRES_PASSWORD': POSTGRES_PARAMS['password'],
        })
    docker_helper.start_container(
        POSTGRES_PARAMS['service'],
        r'database system is ready to accept connections')


def setup_amqp(docker_helper):
    docker_helper.pull_image_if_not_found(RABBITMQ_IMAGE)

    docker_helper.create_container(
        RABBITMQ_PARAMS['service'], RABBITMQ_IMAGE, environment={
            'RABBITMQ_DEFAULT_VHOST': RABBITMQ_PARAMS['vhost'],
            'RABBITMQ_DEFAULT_USER': RABBITMQ_PARAMS['user'],
            'RABBITMQ_DEFAULT_PASS': RABBITMQ_PARAMS['password'],
        })
    docker_helper.start_container(
        RABBITMQ_PARAMS['service'], r'Server startup complete')


def tearDownModule():
    docker_helper.teardown()


def create_django_bootstrap_container(
        docker_helper, name, command=None, publish_port=True):
    kwargs = {
        'command': command,
        'environment': {
            'SECRET_KEY': 'secret',
            'ALLOWED_HOSTS': 'localhost,127.0.0.1,0.0.0.0',
            'DATABASE_URL': DATABASE_URL,
            'CELERY_BROKER_URL': BROKER_URL,
        },
    }
    if publish_port:
        kwargs['ports'] = {'8000/tcp': ('127.0.0.1',)}

    docker_helper.create_container(name, DJANGO_BOOTSTRAP_IMAGE, **kwargs)


class TestWeb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        create_django_bootstrap_container(docker_helper, 'web')
        docker_helper.start_container('web', r'Booting worker')

        cls.web_container = docker_helper.get_container('web')
        cls.web_port = docker_helper.get_container_host_port('web', '8000/tcp')

#    @classmethod
#    def setup_worker(cls):
#        cls.create_web_service_container(
#            'worker', command=['celery', 'worker'], publish_port=False)
#        cls.start_service_container('worker', r'celery@\w+ ready')

#    @classmethod
#    def setup_beat(cls):
#        cls.create_web_service_container(
#            'beat', command=['celery', 'beat'], publish_port=False)
#        cls.start_service_container('beat', r'beat: Starting\.\.\.')

    def get(self, path, **kwargs):
        return requests.get(
            'http://127.0.0.1:{}{}'.format(self.web_port, path), **kwargs)

    def test_admin_site_live(self):
        """
        When we get the /admin/ path, we should receive some HTML for the
        Django admin interface.
        """
        response = self.get('/admin/')

        assert_that(response.headers['Content-Type'],
                    Equals('text/html; charset=utf-8'))
        assert_that(response.text,
                    Contains('<title>Log in | Django site admin</title>'))

    def test_nginx_access_logs(self):
        """
        When a request has been made to the container, Nginx logs access logs
        to stdout
        """
        logs = (self.web_container
                .logs(stdout=True, stderr=False).decode('utf-8'))

        match = re.search(r'\{ "time": .+', logs)
        assert_that(match, Not(Is(None)))

        access_json = json.loads(match.group(0))

        now = datetime.now(timezone.utc)
        assert_that(access_json, MatchesDict({
            # Assert time is valid and recent
            'time': After(iso8601.parse_date, MatchesAll(
                MatchesAny(LessThan(now), Equals(now)),
                MatchesAny(GreaterThan(now - timedelta(seconds=5)))
            )),

            # FIXME: These assertions rely on the previous test running first
            'request': Equals('GET /admin/ HTTP/1.1'),
            'status': Equals(302),
            'body_bytes_sent': Equals(0),
            'request_time': LessThan(1.0),
            'http_referer': Equals(''),

            # Assert remote_addr is an IPv4 (roughly)
            'remote_addr': MatchesRegex(
                r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$'),
            'http_host': Equals('127.0.0.1:{}'.format(self.web_port)),
            'http_user_agent': MatchesRegex(r'^python-requests/'),

            # Not very interesting empty fields
            'remote_user': Equals(''),
            'http_via': Equals(''),
            'http_x_forwarded_proto': Equals(''),
            'http_x_forwarded_for': Equals(''),
        }))

    def test_static_file(self):
        """
        When a static file is requested, Nginx should serve the file with the
        correct mime type.
        """
        response = self.get('/static/admin/css/base.css')

        assert_that(response.headers['Content-Type'], Equals('text/css'))
        assert_that(response.text, Contains('DJANGO Admin styles'))

    def test_manifest_static_storage_file(self):
        """
        When a static file that was processed by Django's
        ManifestStaticFilesStorage system is requested, that file should be
        served with a far-future 'Cache-Control' header.
        """
        hashed_svg = self.web_container.exec_run(
            ['find', 'static/admin/img', '-regextype', 'posix-egrep', '-regex',
             '.*\.[a-f0-9]{12}\.svg$'])
        test_file = hashed_svg.decode('utf-8').split('\n')[0]

        response = self.get('/' + test_file)

        assert_that(response.headers['Content-Type'], Equals('image/svg+xml'))
        assert_that(response.headers['Cache-Control'],
                    Equals('max-age=315360000, public, immutable'))

    def test_django_compressor_js_file(self):
        """
        When a static JavaScript file that was processed by django_compressor
        is requested, that file should be served with a far-future
        'Cache-Control' header.
        """
        compressed_js = self.web_container.exec_run(
            ['find', 'static/CACHE/js', '-name', '*.js'])
        test_file = compressed_js.decode('utf-8').split('\n')[0]

        response = self.get('/' + test_file)

        assert_that(response.headers['Content-Type'],
                    Equals('application/javascript'))
        assert_that(response.headers['Cache-Control'],
                    Equals('max-age=315360000, public, immutable'))

    def test_django_compressor_css_file(self):
        """
        When a static CSS file that was processed by django_compressor is
        requested, that file should be served with a far-future 'Cache-Control'
        header.
        """
        compressed_js = self.web_container.exec_run(
            ['find', 'static/CACHE/css', '-name', '*.css'])
        test_file = compressed_js.decode('utf-8').split('\n')[0]

        response = self.get('/' + test_file)

        assert_that(response.headers['Content-Type'], Equals('text/css'))
        assert_that(response.headers['Cache-Control'],
                    Equals('max-age=315360000, public, immutable'))

    def test_gzip_css_compressed(self):
        """
        When a CSS file larger than 1024 bytes is requested and the
        'Accept-Encoding' header lists gzip as an accepted encoding, the file
        should be served gzipped.
        """
        css_to_gzip = self.web_container.exec_run(
            ['find', 'static', '-name', '*.css', '-size', '+1024c'])
        test_file = css_to_gzip.decode('utf-8').split('\n')[0]

        response = self.get('/' + test_file,
                            headers={'Accept-Encoding': 'gzip'})

        assert_that(response.headers['Content-Type'], Equals('text/css'))
        assert_that(response.headers['Content-Encoding'], Equals('gzip'))
        assert_that(response.headers['Vary'], Equals('Accept-Encoding'))

    def test_gzip_woff_not_compressed(self):
        """
        When a .woff file larger than 1024 bytes is requested and the
        'Accept-Encoding' header lists gzip as an accepted encoding, the file
        should not be served gzipped as it is already a compressed format.
        """
        woff_to_not_gzip = self.web_container.exec_run(
            ['find', 'static', '-name', '*.woff', '-size', '+1024c'])
        test_file = woff_to_not_gzip.decode('utf-8').split('\n')[0]

        response = self.get('/' + test_file,
                            headers={'Accept-Encoding': 'gzip'})

        assert_that(response.headers['Content-Type'],
                    Equals('application/font-woff'))
        assert_that(response.headers, MatchesAll(
            Not(Contains('Content-Encoding')),
            Not(Contains('Vary')),
        ))

    def test_gzip_accept_encoding_respected(self):
        """
        When a CSS file larger than 1024 bytes is requested and the
        'Accept-Encoding' header does not list gzip as an accepted encoding,
        the file should not be served gzipped, but the 'Vary' header should be
        set to 'Accept-Encoding'.
        """
        css_to_gzip = self.web_container.exec_run(
            ['find', 'static', '-name', '*.css', '-size', '+1024c'])
        test_file = css_to_gzip.decode('utf-8').split('\n')[0]

        response = self.get('/' + test_file,
                            headers={'Accept-Encoding': ''})

        assert_that(response.headers['Content-Type'], Equals('text/css'))
        assert_that(response.headers, Not(Contains('Content-Encoding')))
        # The Vary header should be set if there is a *possibility* that this
        # file will be served with a different encoding.
        assert_that(response.headers['Vary'], Equals('Accept-Encoding'))

    def test_gzip_via_compressed(self):
        """
        When a CSS file larger than 1024 bytes is requested and the
        'Accept-Encoding' header lists gzip as an accepted encoding and the
        'Via' header is set, the file should be served gzipped.
        """
        css_to_gzip = self.web_container.exec_run(
            ['find', 'static', '-name', '*.css', '-size', '+1024c'])
        test_file = css_to_gzip.decode('utf-8').split('\n')[0]

        response = self.get(
            '/' + test_file,
            headers={'Accept-Encoding': 'gzip', 'Via': 'Internet.org'})

        assert_that(response.headers['Content-Type'], Equals('text/css'))
        assert_that(response.headers['Content-Encoding'], Equals('gzip'))
        assert_that(response.headers['Vary'], Equals('Accept-Encoding'))

    def test_gzip_small_file_not_compressed(self):
        """
        When a CSS file smaller than 1024 bytes is requested and the
        'Accept-Encoding' header lists gzip as an accepted encoding, the file
        should not be served gzipped.
        """
        css_to_gzip = self.web_container.exec_run(
            ['find', 'static', '-name', '*.css', '-size', '-1024c'])
        test_file = css_to_gzip.decode('utf-8').split('\n')[0]

        response = self.get('/' + test_file,
                            headers={'Accept-Encoding': 'gzip'})

        assert_that(response.headers['Content-Type'], Equals('text/css'))
        assert_that(response.headers, MatchesAll(
            Not(Contains('Content-Encoding')),
            Not(Contains('Vary')),
        ))


if __name__ == '__main__':
    # FIXME: Maybe pytest is better at this
    if len(sys.argv) > 1:
        DJANGO_BOOTSTRAP_IMAGE = sys.argv.pop()

    unittest.main()
