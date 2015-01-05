# Copyright Hybrid Logic Ltd.  See LICENSE file for details.

"""
Tests for :module:`flocker.node.script`.
"""

from StringIO import StringIO

from zope.interface import implementer

from twisted.test.proto_helpers import MemoryReactor
from twisted.internet.interfaces import IReactorCore
from twisted.internet.defer import Deferred
from twisted.trial.unittest import SynchronousTestCase
from twisted.python.usage import UsageError
from twisted.python.filepath import FilePath
from twisted.application.service import Service
from twisted.web.server import Site

from yaml import safe_dump, safe_load
from ...testtools import StandardOptionsTestsMixin
from ...volume.testtools import make_volume_options_tests
from ...route import make_memory_network

from ..script import (
    ServeOptions, ServeScript,
    ChangeStateOptions, ChangeStateScript,
    ReportStateOptions, ReportStateScript)
from .._docker import FakeDockerClient, Unit
from .._deploy import Deployer
from .._model import Application, Deployment, DockerImage, Node, AttachedVolume

from ...volume.testtools import create_volume_service


class ChangeStateScriptTests(SynchronousTestCase):
    """
    Tests for ``ChangeStateScript``.
    """
    def test_deployer_docker_client(self):
        """
        ``ChangeState._docker_client`` is configured with the default docker
        client.
        """
        self.assertIs(None, ChangeStateScript()._docker_client)


class ChangeStateScriptMainTests(SynchronousTestCase):
    """
    Tests for ``ChangeStateScript.main``.
    """
    def test_main_calls_deployer_change_node_state(self):
        """
        ``ChangeStateScript.main`` calls ``Deployer.change_node_state`` with
        the ``Deployment`` and `hostname` supplied on the command line.
        """
        script = ChangeStateScript()

        change_node_state_calls = []

        def spy_change_node_state(self, desired_state, current_cluster_state,
                                  hostname):
            """
            A stand in for ``Deployer.change_node_state`` which records calls
            made to it.
            """
            change_node_state_calls.append((desired_state,
                                            current_cluster_state, hostname))

        self.patch(
            Deployer, 'change_node_state', spy_change_node_state)

        expected_deployment = object()
        expected_current = object()
        expected_hostname = b'node1.example.com'
        options = dict(deployment=expected_deployment,
                       current=expected_current,
                       hostname=expected_hostname)
        script.main(
            reactor=object(), options=options, volume_service=Service())

        self.assertEqual(
            [(expected_deployment, expected_current, expected_hostname)],
            change_node_state_calls
        )


class StandardChangeStateOptionsTests(
        make_volume_options_tests(
            ChangeStateOptions, extra_arguments=[
                safe_dump(dict(version=1, nodes={})),
                safe_dump(dict(version=1, applications={})),
                safe_dump({}),
                b"node001",
            ])):
    """
    Tests for the volume configuration arguments of ``ChangeStateOptions``.
    """


class ChangeStateOptionsTests(StandardOptionsTestsMixin, SynchronousTestCase):
    """
    Tests for :class:`ChangeStateOptions`.
    """
    options = ChangeStateOptions

    def test_custom_configs(self):
        """
        The supplied application and deployment configuration strings are
        parsed as a :class:`Deployment` on the options instance.
        """
        application = Application(
            name=u'mysql-hybridcluster',
            image=DockerImage(repository=u'hybridlogic/mysql5.9',
                              tag=u'latest'),
            ports=frozenset(),
            links=frozenset(),
            )

        node = Node(hostname='node1.example.com',
                    applications=frozenset([application]))
        options = self.options()
        deployment_config = {"nodes": {node.hostname: [application.name]},
                             "version": 1}

        application_config = dict(
            version=1,
            applications={
                application.name: {'image': 'hybridlogic/mysql5.9:latest'}
            }
        )

        current_config = {'node2.example.com': {
            'applications': {
                'mysql-something': {
                    'image': 'unknown',
                    'volume': {'mountpoint': b'/var/lib/data'},
                }
            },
            'version': 1
        }}

        options.parseOptions(
            [safe_dump(deployment_config),
             safe_dump(application_config),
             safe_dump(current_config),
             b'node1.example.com'])

        self.assertEqual(
            Deployment(nodes=frozenset([node])), options['deployment'])

    def test_current_configuration(self):
        """
        The supplied current cluster configuration strings is parsed as a
        :class:`Deployment` on the options instance.
        """
        options = self.options()
        deployment_config = {"nodes": {},
                             "version": 1}

        application_config = dict(
            version=1,
            applications={},
        )

        current_config = {'node2.example.com': {
            'applications': {
                'mysql-something': {
                    'image': 'unknown',
                    'volume': {'mountpoint': b'/var/lib/data'},
                }
            },
            'version': 1
        }}

        expected_current_config = Deployment(nodes=frozenset([
            Node(hostname='node2.example.com', applications=frozenset([
                Application(
                    name='mysql-something',
                    image=DockerImage.from_string('unknown'),
                    ports=frozenset(),
                    links=frozenset(),
                    volume=AttachedVolume(
                        name='mysql-something',
                        mountpoint=FilePath(b'/var/lib/data'),
                    )
                ),
            ]))]))

        options.parseOptions(
            [safe_dump(deployment_config),
             safe_dump(application_config),
             safe_dump(current_config),
             b'node1.example.com'])

        self.assertEqual(expected_current_config, options['current'])

    def test_configuration_error(self):
        """
        If the supplied configuration strings are valid `YAML` but are not
        valid, a ``UsageError`` is raised with a string representation of the
        error.
        """
        options = self.options()
        application = Application(
            name=u'mysql-hybridcluster',
            image=DockerImage(
                repository=u'hybridlogic/mysql5.9', tag=u'latest'),
        )

        node = Node(hostname='node1.example.com',
                    applications=frozenset([application]))
        options = self.options()
        deployment_config = {"nodes": {node.hostname: [application.name]},
                             "version": 1}

        exception = self.assertRaises(
            UsageError,
            options.parseOptions,
            [safe_dump(deployment_config), safe_dump({}), safe_dump({}),
             b'node1.example.com']
        )

        self.assertEqual(
            str(exception),
            ("Configuration Error: "
             "Application configuration has an error. Missing "
             "'applications' key.")
        )

    def test_invalid_deployment_yaml(self):
        """
        If the supplied deployment_config is not valid `YAML`, a ``UsageError``
        is raised.
        """
        options = self.options()
        deployment_bad_yaml = "{'foo':'bar', 'x':y, '':'"
        e = self.assertRaises(
            UsageError, options.parseOptions,
            [deployment_bad_yaml, b'', b'{}', b'node1.example.com'])

        # See https://github.com/ClusterHQ/flocker/issues/282 for more complete
        # testing of this string.
        self.assertTrue(
            str(e).startswith('Deployment config could not be parsed as YAML')
        )

    def test_invalid_application_yaml(self):
        """
        If the supplied application_config is not valid `YAML`, a
        ``UsageError`` is raised.
        """
        options = self.options()
        application_bad_yaml = "{'foo':'bar', 'x':y, '':'"
        e = self.assertRaises(
            UsageError, options.parseOptions,
            [b'', application_bad_yaml, b'{}', b'node1.example.com'])

        # See https://github.com/ClusterHQ/flocker/issues/282 for more complete
        # testing of this string.
        self.assertTrue(
            str(e).startswith('Application config could not be parsed as YAML')
        )

    def test_invalid_current_yaml(self):
        """
        If the supplied current config is not valid `YAML`, a
        ``UsageError`` is raised.
        """
        options = self.options()
        bad_yaml = "{'foo':'bar', 'x':y, '':'"
        e = self.assertRaises(
            UsageError, options.parseOptions,
            [b'', b'', bad_yaml, b'node1.example.com'])

        # See https://github.com/ClusterHQ/flocker/issues/282 for more complete
        # testing of this string.
        self.assertTrue(
            str(e).startswith('Current config could not be parsed as YAML')
        )

    def test_hostname_key(self):
        """
        The supplied hostname is assigned to a `hostname` key.
        """
        expected_hostname = u'foobar.example.com'
        options = self.options()
        options.parseOptions(
            [b'{nodes: {}, version: 1}',
             b'{applications: {}, version: 1}',
             b'{}',
             expected_hostname.encode('ascii')])
        self.assertEqual(
            (expected_hostname, unicode),
            (options['hostname'], type(options['hostname']))
        )

    def test_nonascii_hostname(self):
        """
        A ``UsageError`` is raised if the supplied hostname is not ASCII
        encoded.
        """
        hostname = u'\xa3'.encode('utf8')
        options = self.options()
        e = self.assertRaises(
            UsageError,
            options.parseOptions,
            [b'{nodes: {}, version: 1}',
             b'{applications: {}, version: 1}',
             b'{}',
             hostname]
        )

        self.assertEqual(
            "Non-ASCII hostname: {hostname}".format(hostname=hostname),
            str(e)
        )


class StandardReportStateOptionsTests(
        make_volume_options_tests(ReportStateOptions)):
    """
    Tests for the volume configuration arguments of ``ReportStateOptions``.
    """


class ReportStateOptionsTests(StandardOptionsTestsMixin, SynchronousTestCase):
    """
    Tests for :class:`ReportStateOptions`.
    """
    options = ReportStateOptions

    def test_no_options(self):
        """
        ``ReportStateOptions`` can instantiate and successfully parse
        without any (non-standard) options.
        """
        options = self.options()
        options.parseOptions([])

    def test_wrong_number_options(self):
        """
        If any additional arguments are supplied, a ``UsageError`` is raised.
        """
        options = self.options()
        e = self.assertRaises(
            UsageError,
            options.parseOptions,
            ['someparameter']
        )
        self.assertEqual(str(e), b"Wrong number of arguments.")


class ReportStateScriptMainTests(SynchronousTestCase):
    """
    Tests for ``ReportStateScript.main``.
    """
    def test_yaml_output(self):
        """
        ``ReportStateScript.main`` returns a deferred which fires after the
        YAML representation of the node state, including applications (running
        or not) and used TCP port numbers from
        ``Deployer.discover_node_configuration``, have been written to stdout.
        """
        unit1 = Unit(name=u'site-example.com',
                     container_name=u'site-example.com',
                     container_image=u'clusterhq/wordpress:latest',
                     activation_state=u'active')
        unit2 = Unit(name=u'site-example.net',
                     container_name=u'site-example.net',
                     container_image=u'clusterhq/wordpress:latest',
                     activation_state=u'inactive')
        units = {unit1.name: unit1, unit2.name: unit2}

        fake_docker = FakeDockerClient(units=units)

        used_ports = frozenset([1, 10, 200, 52000])
        network = make_memory_network(used_ports=used_ports)

        expected = {
            'used_ports': sorted(used_ports),
            'applications': {
                'site-example.net': {
                    'image': unit2.container_image,
                    'restart_policy': {'name': 'never'},
                },
                'site-example.com': {
                    'image': unit1.container_image,
                    'restart_policy': {'name': 'never'},
                }
            },
            'version': 1,
        }

        script = ReportStateScript(fake_docker, network)
        content = StringIO()
        self.patch(script, '_stdout', content)
        script.main(
            reactor=object(), options=[],
            volume_service=create_volume_service(self))
        self.assertEqual(safe_load(content.getvalue()), expected)


# TODO: This should be provided by Twisted (also it should be more complete
# instead of 1/3rd done).
from twisted.internet.base import _ThreePhaseEvent


@implementer(IReactorCore)
class MemoryCoreReactor(MemoryReactor):
    """
    Just enough of an implementation of IReactorCore to pass to
    ``_main_for_service`` in the unit tests.
    """
    def __init__(self):
        MemoryReactor.__init__(self)
        self._triggers = {}

    def addSystemEventTrigger(self, phase, eventType, callable, *args, **kw):
        event = self._triggers.setdefault(eventType, _ThreePhaseEvent())
        event.addTrigger(phase, callable, *args, **kw)
        # removeSystemEventTrigger isn't implemented so the return value here
        # isn't useful.
        return object()

    def fireSystemEvent(self, eventType):
        event = self._triggers.get(eventType)
        if event is not None:
            event.fireEvent()


class AsyncStopService(Service):
    """
    An ``IService`` implementation which can return an unfired ``Deferred``
    from its ``stopService`` method.

    :ivar Deferred stop_result: The object to return from ``stopService``.
        ``AsyncStopService`` won't do anything more than return it.  If it is
        ever going to fire, some external code is responsible for firing it.
    """
    def __init__(self, stop_result):
        self.stop_result = stop_result

    def stopService(self):
        Service.stopService(self)
        return self.stop_result


class ServeScriptMainTests(SynchronousTestCase):
    """
    Tests for ``ServeScript.main``.
    """
    def setUp(self):
        self.reactor = MemoryCoreReactor()
        self.service = Service()
        self.script = ServeScript()

    def main(self, reactor, service):
        options = ServeOptions()
        options.parseOptions([])
        return self.script.main(reactor, options, service)

    def _shutdown_reactor(self, reactor):
        """
        Simulate reactor shutdown.

        :param IReactorCore reactor: The reactor to shut down.
        """
        reactor.fireSystemEvent("shutdown")

    def test_starts_service(self):
        """
        ``ServeScript.main`` accepts an ``IService`` provider and starts it.
        """
        self.main(self.reactor, self.service)
        self.assertTrue(
            self.service.running, "The service should have been started.")

    def test_returns_unfired_deferred(self):
        """
        ``ServeScript.main`` returns a ``Deferred`` which has not fired.
        """
        result = self.main(self.reactor, self.service)
        self.assertNoResult(result)

    def test_fire_on_stop(self):
        """
        The ``Deferred`` returned by ``ServeScript.main`` fires with ``None``
        when the reactor is stopped.
        """
        result = self.main(self.reactor, self.service)
        self._shutdown_reactor(self.reactor)
        self.assertIs(None, self.successResultOf(result))

    def test_stops_service(self):
        """
        When the reactor is stopped, ``ServeScript.main`` stops the service it
        was called with.
        """
        self.main(self.reactor, self.service)
        self._shutdown_reactor(self.reactor)
        self.assertFalse(
            self.service.running, "The service should have been stopped.")

    def test_wait_for_service_stop(self):
        """
        The ``Deferred`` returned by ``ServeScript.main`` does not fire before
        the ``Deferred`` returned by the service's ``stopService`` method
        fires.
        """
        result = self.main(self.reactor, AsyncStopService(Deferred()))
        self._shutdown_reactor(self.reactor)
        self.assertNoResult(result)

    def test_fire_after_service_stop(self):
        """
        The ``Deferred`` returned by ``ServeScript.main`` fires once the
        ``Deferred`` returned by the service's ``stopService`` method fires.
        """
        async = Deferred()
        result = self.main(self.reactor, AsyncStopService(async))
        self._shutdown_reactor(self.reactor)
        async.callback(None)
        self.assertIs(None, self.successResultOf(result))

    def test_starts_http_api_server(self):
        """
        ``ServeScript.main`` starts a HTTP server on the given port.
        """
        self.script.main(self.reactor, {"port": 8001}, self.service)
        server = self.reactor.tcpServers[0]
        port = server[0]
        factory = server[1].__class__
        self.assertEqual((port, factory), (8001, Site))


class StandardServeOptionsTests(
        make_volume_options_tests(ServeOptions)):
    """
    Tests for the volume configuration arguments of ``ServeOptions``.
    """
    def test_default_port(self):
        """
        The default port configured by ``ServeOptions`` is 4523.
        """
        options = ServeOptions()
        options.parseOptions([])
        self.assertEqual(options["port"], 4523)

    def test_custom_port(self):
        """
        The ``--port`` command-line option allows configuring the port.
        """
        options = ServeOptions()
        options.parseOptions(["--port", 1234])
        self.assertEqual(options["port"], 1234)
