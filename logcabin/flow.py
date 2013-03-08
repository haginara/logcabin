from common import SimpleStage, MultiStage
from util import BroadcastQueue

class Fanin(MultiStage):
    """
    This merges all of the outputs of the child stages to a single queue.

    Syntax::

        with Fanin():
            Udp()
            Zeromq()
    """

    def setup(self, q):
        self.output = q
        for s in self.stages:
            s.setup(self.output)

class Sequence(MultiStage):
    """
    This connects the output of the preceding stage to the input of the next,
    and so on, so the event is processed by each stage one after the other, in
    order.

    Syntax::

        with Sequence():
            Mutate()
            Mutate()
            ...
    """

    def setup(self, q):
        self.output = q
        # this is setup backwards, so the output from the current is
        # connected to the input of the successor, and the input of the current
        # will be connected to the output of the predecessor.
        for s in reversed(self.stages):
            q = s.setup(q)
        self.input = q

class Fanout(MultiStage):
    """
    This enqueues the event onto multiple input queues in parallel.

    Syntax::

        with Fanout():
            Log()
            Elasticsearch()
            Mongodb()
            ...
    """

    def setup(self, q):
        self.output = q
        queues = []
        for s in self.stages:
            queues.append(s.setup(q))
        self.input = BroadcastQueue(queues)
        return self.input

from contextlib import contextmanager

class DefaultDictProxy(object):
    """Proxies attribute request to dict, missing keys default to None"""
    def __init__(self, d):
        self.d = d

    def __getattr__(self, k):
        # properties on event (eg .tags) take preference over elements
        return self[k]

    def __getitem__(self, k):
        if hasattr(self.d, k):
            return getattr(self.d, k)
        else:
            return self.d.get(k)

class Switch(SimpleStage):
    """Branch flow based on a condition.

    The cases are specified using this syntax. The condition may be a lambda
    expression or code string::

        with Switch() as case:
            with case(lambda ev: ev.field == 'value'):
                Json()
            with case('field2 == "value2"'):
                Mutate()
            with case.default:
                Regex(regex='abc')
    """

    def __init__(self, on_error='reject'):
        super(Switch, self).__init__(on_error=on_error)
        self.cases = []

    # configuration contexts

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        pass

    @contextmanager
    def __call__(self, condition):
        if isinstance(condition, str):
            # python code as string
            code = compile(condition, 'string', 'eval')
            condition = lambda ev: eval(code, {}, ev)

        br = Sequence()
        self.cases.append((condition, br))
        with br:
            yield

    @property
    def default(self):
        return self(lambda x: True)

    def setup(self, q):
        # separate queue for the incoming condition,
        # and fan in on the individual pipelines.
        for t, br in self.cases:
            br.setup(q)
        return super(Switch, self).setup(q)

    def start(self):
        super(Switch, self).start()
        # start sub-pipelines
        for t, br in self.cases:
            br.start()

    def process(self, event):
        # pass the event into the sub-queue for the applicable pipeline
        ret = True
        for case, br in self.cases:
            if case(DefaultDictProxy(event)):
                br.input.put(event)
                ret = False
                break

        # if no condition handles it, pass straight on (True)
        return ret

class If(SimpleStage):
    """
    Conditionally execute stages.

    The syntax is as follows. The condition may be a lambda expression or code
    string::

        with If('field==1'):
            Json()
    """

    def __init__(self, condition, on_error='reject'):
        super(If, self).__init__(on_error=on_error)
        if isinstance(condition, str):
            # python code as string
            code = compile(condition, 'string', 'eval')
            condition = lambda ev: eval(code, {}, ev)
        self.condition = condition

    # configuration contexts

    def __enter__(self):
        self.branch = Sequence()
        return self.branch.__enter__()

    def __exit__(self, *exc_info):
        return self.branch.__exit__()

    def setup(self, q):
        # pass output queue to the branch
        self.branch.setup(q)
        return super(If, self).setup(q)

    def start(self):
        super(If, self).start()
        # start sub-pipelines
        self.branch.start()

    def process(self, event):
        # pass the event into the sub-queue for the applicable pipeline
        if self.condition(DefaultDictProxy(event)):
            self.branch.input.put(event)
            # sub-pipeline will dequeue
            return False
        else:
            # if condition False, pass straight on (True)
            return True