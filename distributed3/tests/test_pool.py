from operator import add
from time import time, sleep
from toolz import merge

from distributed3 import Center, Worker, Pool
from distributed3.pool import divide_tasks, RemoteData
from distributed3.utils import ignoring
from distributed3.core import (connect_sync, read_sync, write_sync,
        send_recv_sync)
from contextlib import contextmanager
from multiprocessing import Process

from tornado import gen
from tornado.ioloop import IOLoop


def _test_cluster(f):
    @gen.coroutine
    def g():
        c = Center('127.0.0.1', 8017)
        c.listen(c.port)
        a = Worker('127.0.0.1', 8018, c.ip, c.port, ncores=1)
        yield a._start()
        b = Worker('127.0.0.1', 8019, c.ip, c.port, ncores=1)
        yield b._start()

        p = Pool(c.ip, c.port)

        while len(c.ncores) < 2:
            yield gen.sleep(0.01)

        yield p._sync_center()

        try:
            yield f(c, a, b, p)
        finally:
            with ignoring():
                yield p._close_connections()
            with ignoring():
                yield a._close()
            with ignoring():
                yield b._close()
            c.stop()

    IOLoop.current().run_sync(g)


def test_pool():
    @gen.coroutine
    def f(c, a, b, p):
        computation = yield p._apply_async(add, [1, 2])
        assert computation.status == b'running'
        assert set(p.available_cores.values()) == set([0, 1])
        x = yield computation._get()
        assert computation.status == x.status == b'success'
        assert list(p.available_cores.values()) == [1, 1]
        result = yield x._get()
        assert result == 3

        computation = yield p._apply_async(add, [x, 10])
        y = yield computation._get()
        result = yield y._get()
        assert result == 13

        assert set((len(a.data), len(b.data))) == set((0, 2))

        x = yield p._apply_async(add, [1, 2])
        y = yield p._apply_async(add, [1, 2])
        assert list(p.available_cores.values()) == [0, 0]
        xx = yield x._get()
        yield xx._get()
        assert set(p.available_cores.values()) == set([0, 1])
        yy = yield y._get()
        yield yy._get()
        assert list(p.available_cores.values()) == [1, 1]

        seq = yield p._map(lambda x: x * 100, [1, 2, 3])
        result = yield seq[0]._get(False)
        assert result == 100
        result = yield seq[1]._get(False)
        assert result == 200
        result = yield seq[2]._get(True)
        assert result == 300

        # Handle errors gracefully
        results = yield p._map(lambda x: 3 / x, [0, 1, 2, 3])
        assert all(isinstance(result, RemoteData) for result in results)
        try:
            yield results[0]._get()
            assert False
        except ZeroDivisionError:
            pass

    _test_cluster(f)


def test_pool_inputs():
    p = Pool('127.0.0.1:8000')
    assert p.center_ip == '127.0.0.1'
    assert p.center_port == 8000


def test_workshare():
    who_has = {'x': {'Alice'},
               'y': {'Alice', 'Bob'},
               'z': {'Bob'}}
    needed = {1: {'x'},
              2: {'y'},
              3: {'z'},
              4: {'x', 'z'},
              5: set()}

    shares, extra = divide_tasks(who_has, needed)
    assert shares == {'Alice': [2, 1], 'Bob': [2, 3]}
    assert extra == {4, 5}


def run_center(port):
    from distributed3 import Center
    from tornado.ioloop import IOLoop
    center = Center('127.0.0.1', port)
    center.listen(port)
    IOLoop.current().start()
    IOLoop.current().close()


def run_worker(port, center_port, **kwargs):
    from distributed3 import Worker
    from tornado.ioloop import IOLoop
    worker = Worker('127.0.0.1', port, '127.0.0.1', center_port, **kwargs)
    worker.start()
    IOLoop.current().start()
    IOLoop.current().close()


@contextmanager
def cluster():
    center = Process(target=run_center, args=(8010,))
    a = Process(target=run_worker, args=(8011, 8010), kwargs={'ncores': 1})
    b = Process(target=run_worker, args=(8012, 8010), kwargs={'ncores': 1})

    center.start()
    a.start()
    b.start()

    sock = connect_sync('127.0.0.1', 8010)
    while True:
        write_sync(sock, {'op': 'ncores'})
        ncores = read_sync(sock)
        if len(ncores) == 2:
            break

    try:
        yield {'proc': center, 'port': 8010}, [{'proc': a, 'port': 8011},
                                               {'proc': b, 'port': 8012}]
    finally:
        with ignoring():
            a.terminate()
        with ignoring():
            b.terminate()
        with ignoring():
            center.terminate()


def test_cluster():
    with cluster() as (c, [a, b]):
        pass


def test_pool_synchronous():
    with cluster() as (c, [a, b]):
        pool = Pool('127.0.0.1', c['port'])
        pool.sync_center()
        assert pool.available_cores == {('127.0.0.1', a['port']): 1,
                                        ('127.0.0.1', b['port']): 1}

        data = pool.map(lambda x: x * 10, [1, 2, 3])
        results = pool.gather(data)
        assert results == [10, 20, 30]


def test_close_worker_cleanly_before_map():
    with cluster() as (c, [a, b]):
        p = Pool('127.0.0.1', c['port'])

        send_recv_sync('127.0.0.1', a['port'], op='terminate')

        while len(send_recv_sync('127.0.0.1', c['port'], op='ncores')) > 1:
            sleep(0.01)

        result = p.map(lambda x: x + 1, range(3))

        assert list(p.available_cores.keys()) == [('127.0.0.1', b['port'])]
        p.close()


def test_collect_from_dead_worker():
    @gen.coroutine
    def f(c, a, b, p):
        remote = yield p._scatter(range(10))
        yield a._close()
        try:
            local = yield p._gather(remote)
            assert False
        except KeyError:
            pass

    _test_cluster(f)


def test_failing_job():
    def g(x):
        return 1 / x

    @gen.coroutine
    def f(c, a, b, p):
        results = yield p._map(g, [2, 1, 0])

        result = yield results[0]._get()
        assert result == 1 / 2

        try:
            yield results[2]._get()
            assert False
        except ZeroDivisionError as e:
            pass

    _test_cluster(f)