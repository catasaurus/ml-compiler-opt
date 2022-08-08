# coding=utf-8
# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Local Process Pool - based middleware implementation.

This is a simple implementation of a worker pool, running on the local machine.
Each worker object is hosted by a separate process. Each worker object may
handle a number of concurrent requests. The client is given a stub object that
exposes the same methods as the worker, just that they return Futures.

There is a bidirectional pipe between a stub and its corresponding
process/worker. One direction is used to place tasks (method calls), the other
to place results. Tasks and results are correlated by a monotonically
incrementing counter maintained by the stub.

The worker process dequeues tasks promptly and either re-enqueues them to a
local thread pool, or, if the task is 'urgent', it executes it promptly.
"""
import concurrent.futures
import dataclasses
import functools
import multiprocessing
import threading

from absl import logging
# pylint: disable=unused-import
from compiler_opt.distributed.worker import Worker

from contextlib import AbstractContextManager
from multiprocessing import connection
from typing import Any, Callable, Dict, Optional


@dataclasses.dataclass(frozen=True)
class Task:
  msgid: int
  func_name: str
  args: tuple
  kwargs: dict
  is_urgent: bool


@dataclasses.dataclass(frozen=True)
class TaskResult:
  msgid: int
  success: bool
  value: Any


def _run_impl(pipe: connection.Connection, worker_class: 'type[Worker]', *args,
              **kwargs):
  """Worker process entrypoint."""

  # A setting of 1 does not inhibit the while loop below from running since
  # that runs on the main thread of the process. Urgent tasks will still
  # process near-immediately. `threads` only controls how many threads are
  # spawned at a time which execute given tasks. In the typical clang-spawning
  # jobs, this effectively limits the number of clang instances spawned.
  pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
  obj = worker_class(*args, **kwargs)

  # Pipes are not thread safe
  pipe_lock = threading.Lock()

  def send(task_result: TaskResult):
    with pipe_lock:
      pipe.send(task_result)

  def make_ondone(msgid):

    def on_done(f: concurrent.futures.Future):
      if f.exception():
        send(TaskResult(msgid=msgid, success=False, value=f.exception()))
      else:
        send(TaskResult(msgid=msgid, success=True, value=f.result()))

    return on_done

  # Run forever. The stub will just kill the runner when done.
  while True:
    task: Task = pipe.recv()
    the_func = getattr(obj, task.func_name)
    application = functools.partial(the_func, *task.args, **task.kwargs)
    if task.is_urgent:
      try:
        res = application()
        send(TaskResult(msgid=task.msgid, success=True, value=res))
      except BaseException as e:  # pylint: disable=broad-except
        send(TaskResult(msgid=task.msgid, success=False, value=e))
    else:
      pool.submit(application).add_done_callback(make_ondone(task.msgid))


def _run(*args, **kwargs):
  try:
    _run_impl(*args, **kwargs)
  except BaseException as e:
    logging.error(e)
    raise e


def _make_stub(cls: 'type[Worker]', *args, **kwargs):

  class _Stub:
    """Client stub to a worker hosted by a process."""

    def __init__(self):
      parent_pipe, child_pipe = multiprocessing.get_context().Pipe()
      self._pipe = parent_pipe
      self._pipe_lock = threading.Lock()

      # this is the process hosting one worker instance.
      # we set aside 1 thread to coordinate running jobs, and the main thread
      # to handle high priority requests. The expectation is that the user
      # achieves concurrency through multiprocessing, not multithreading.
      self._process = multiprocessing.get_context().Process(
          target=functools.partial(
              _run, worker_class=cls, pipe=child_pipe, *args, **kwargs))
      # lock for the msgid -> reply future map. The map will be set to None
      # when we stop.
      self._lock = threading.Lock()
      self._map: Dict[int, concurrent.futures.Future] = {}

      # thread draining the pipe
      self._pump = threading.Thread(target=self._msg_pump)

      # Set the state of this worker to "dead" if the process dies naturally.
      def observer():
        self._process.join()
        # Feed the parent pipe a poison pill, this kills msg_pump
        child_pipe.send(None)

      self._observer = threading.Thread(target=observer)

      # atomic control to _msgid
      self._msgidlock = threading.Lock()
      self._msgid = 0

      # start the worker and the message pump
      self._process.start()
      # the observer must follow the process start, otherwise join() raises.
      self._observer.start()
      self._pump.start()

    def _msg_pump(self):
      while True:
        task_result: Optional[TaskResult] = self._pipe.recv()
        if task_result is None:  # Poison pill fed by observer
          break
        with self._lock:
          future = self._map[task_result.msgid]
          del self._map[task_result.msgid]
          if task_result.success:
            future.set_result(task_result.value)
          else:
            future.set_exception(task_result.value)

      # clear out pending futures and mark ourselves as "stopped" by null-ing
      # the map
      with self._lock:
        for _, v in self._map.items():
          v.set_exception(concurrent.futures.CancelledError())
        self._map = None

    def _is_stopped(self):
      return self._map is None

    def __getattr__(self, name) -> Callable[[Any], concurrent.futures.Future]:
      result_future = concurrent.futures.Future()

      with self._msgidlock:
        msgid = self._msgid
        self._msgid += 1

      def remote_call(*args, **kwargs):
        with self._lock:
          if self._is_stopped():
            result_future.set_exception(concurrent.futures.CancelledError())
          else:
            with self._pipe_lock:
              self._pipe.send(
                  Task(
                      msgid=msgid,
                      func_name=name,
                      args=args,
                      kwargs=kwargs,
                      is_urgent=cls.is_priority_method(name)))
            self._map[msgid] = result_future
        return result_future

      return remote_call

    def shutdown(self):
      try:
        # Killing the process triggers observer exit, which triggers msg_pump
        # exit
        self._process.kill()
      except:  # pylint: disable=bare-except
        pass

    def join(self):
      self._observer.join()
      self._pump.join()
      self._process.join()

    def __dir__(self):
      return [n for n in dir(cls) if not n.startswith('_')]

  return _Stub()


class LocalWorkerPool(AbstractContextManager):
  """A pool of workers hosted on the local machines, each in its own process."""

  def __init__(self, worker_class: 'type[Worker]', count: Optional[int], *args,
               **kwargs):
    if not count:
      count = multiprocessing.cpu_count()
    self._stubs = [
        _make_stub(worker_class, *args, **kwargs) for _ in range(count)
    ]

  def __enter__(self):
    return self._stubs

  def __exit__(self, *args):
    # first, trigger killing the worker process and exiting of the msg pump,
    # which will also clear out any pending futures.
    for s in self._stubs:
      s.shutdown()
    # now wait for the message pumps to indicate they exit.
    for s in self._stubs:
      s.join()
