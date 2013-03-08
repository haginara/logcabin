from unittest import TestCase
import gevent
from gevent.queue import Queue
import yaml
import datetime

from logcabin.event import Event
from logcabin.context import DummyContext
from logcabin.filters import json, regex, mutate, stats, syslog

from testhelper import assertEventEquals, about, between

class FilterTests(TestCase):
    def create_stage(self, **conf):
        return self.cls(**conf)

    def create(self, conf={}, events=[]):
        if isinstance(conf, str):
            conf = yaml.load(conf)

        self.input = Queue()
        self.output = Queue()
        with DummyContext():
            self.i = self.create_stage(**conf)
        self.input = self.i.setup(self.output)

        self.i.start()
        for ev in events:
            self.input.put(ev)
        return self.i

    def wait(self, timeout=1.0, events=1):
        with gevent.Timeout(timeout):
            # wait for input to be consumed and output to be produced
            while self.input.qsize():
                gevent.sleep(0.0)
            while self.output.qsize() < events:
                gevent.sleep(0.0)

        self.i.stop()
        if events:
            return [self.output.get() for n in xrange(events)]

class JsonTests(FilterTests):
    cls = json.Json

    def test_consume(self):
        self.create({},
            [Event(data='{"a": 1}')])
        q = self.wait()
        assertEventEquals(self, Event(a=1), q[0])

    def test_consume_false(self):
        self.create({'consume': False},
            [Event(data='{"a": 1}')])
        q = self.wait()
        assertEventEquals(self, Event(a=1, data='{"a": 1}'), q[0])

    def test_bad_json(self):
        self.create({'consume': False},
            [Event(data='"invalid')])
        self.wait(events=0)
        self.assertEquals(0, self.output.qsize())

class RegexTests(FilterTests):
    cls = regex.Regex

    def test_match(self):
        self.create({'regex': r'(?P<letters>[a-z]+)(?P<numbers>\d+)'},
            [Event(data='abc123')])
        q = self.wait()
        assertEventEquals(self, Event(letters='abc', numbers='123'), q[0])

    def test_no_match(self):
        self.create({'regex': r'(?P<letters>[a-z]+)(?P<numbers>\d+)',
            'on_error': 'tag'},
            [Event(data='.!$#')])
        q = self.wait()
        self.assertEquals(['_unparsed'], q[0].tags)

class MutateTests(FilterTests):
    cls = mutate.Mutate

    def test_mutate(self):
        self.create({'set': {'a': 2}},
            [Event(a=1)])
        q = self.wait()
        assertEventEquals(self, Event(a=2), q[0])

class StatsTests(FilterTests):
    cls = stats.Stats
    conf = dict(period=0.1, metrics={'rails.{controller}.{action}.{0}': '*'})

    def test_stats(self):
        self.create(StatsTests.conf, [
            Event(controller='home', action='index', duration=3.0, bytes=6926),
            Event(controller='home', action='login', duration=2.4, bytes=15568),
            Event(controller='home', action='index', duration=4.0, bytes=18150),
            Event(controller='home', action='index', duration=3.5, bytes=30159),
            Event(controller='missing', action='duration'),
            Event(someotherevent='blah', duration=3.5),
        ])

        # 8 events expected - the above 6, and then 4 stat events
        q = self.wait(events=10)

        q = [i for i in q if i.stats]
        q.sort(key=lambda k: k.metric)

        expected = Event(metric='rails.home.index.bytes',
            stats={
                'count': 3,
                'rate': between(1, 100),
                'max': 30159,
                'min': 6926,
                'median': 18150,
                'mean': 18411,
                'stddev': about(30789),
                'upper95': 28958.1,
                'upper99': 29918.82,
            },
            tags=['stat'],
        )
        assertEventEquals(self, expected, q[0])

        expected = Event(metric='rails.home.index.duration',
            stats={
                'count': 3,
                'rate': between(1, 100),
                'max': 4.0,
                'min': 3.0,
                'median': 3.5,
                'mean': 3.5,
                'stddev': 5.0,
                'upper95': 3.95,
                'upper99': 3.99,
            },
            tags=['stat'],
        )
        assertEventEquals(self, expected, q[1])

        expected = Event(metric='rails.home.login.duration',
            stats={
                'count': 1,
                'rate': between(1, 100),
                'max': 2.4,
                'min': 2.4,
                'median': 2.4,
                'mean': 2.4,
                'stddev': 0.0,
                'upper95': 2.4,
                'upper99': 2.4,
            },
            tags=['stat'],
        )
        assertEventEquals(self, expected, q[3])

class SyslogTests(FilterTests):
    cls = syslog.Syslog

    # Formats - you can never have enough of them!
    good_packets = [
        '<174>Nov 30 19:56:13 host01 prog[1234]: log message', # RSYSLOG_ForwardFormat
        '<174>Mar  4 11:57:46 micro01 testlog.py: test', # RSYSLOG_TraditionalFileFormat
        '<174>2012-12-07T13:44:27.710956+01:00 test01 program: test' # RSYSLOG_ForwardFormat
    ]
    good_events = [
        Event(
            timestamp=datetime.datetime(2013, 10, 30, 19, 56, 13),
            facility='local5',
            severity='Informational',
            host='host01',
            program='prog',
            pid='1234',
            message='log message'),
        Event(
            timestamp=datetime.datetime(2013, 3, 4, 11, 56, 46),
            facility='local5',
            severity='Informational',
            host='micro01',
            program='testlog.py',
            pid=None,
            message='test'),
        Event(
            timestamp=datetime.datetime(2012, 12, 7, 12, 44, 27, 710956),
            facility='local5',
            severity='Informational',
            host='test01',
            program='program',
            pid=None,
            message='test')

    ]
    bad_packets = ['<>Nov 30 19:56:13 host01 prog[1234]: log message']

    def test_good(self):
        self.create({},
            [Event(data=x) for x in self.good_packets])

        events = self.wait(events=len(self.good_events))
        for ev in self.good_events:
            assertEventEquals(self, ev, events.pop(0))

    def test_bad(self):
        self.create({'consume': False, 'on_error': 'tag'},
            [Event(data=x) for x in self.bad_packets])
        events = self.wait(events=len(self.bad_packets))

        bad_events = [Event(data=x, message='invalid syslog', tags=['_unparsed']) for x in self.bad_packets]
        for ev in bad_events:
            assertEventEquals(self, ev, events.pop(0))