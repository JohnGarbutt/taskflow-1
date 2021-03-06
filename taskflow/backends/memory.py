# -*- coding: utf-8 -*-

# vim: tabstop=4 shiftwidth=4 softtabstop=4

#    Copyright (C) 2012 Yahoo! Inc. All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from datetime import datetime

import functools
import logging
import threading
import weakref

import taskflow

from taskflow import catalog
from taskflow import exceptions as exc
from taskflow import jobboard
from taskflow import logbook
from taskflow import states
from taskflow import utils

LOG = logging.getLogger(__name__)


def check_not_closed(meth):

    @functools.wraps(meth)
    def check(self, *args, **kwargs):
        if self._closed:  # pylint: disable=W0212
            raise exc.ClosedException("Unable to call %s on closed object" %
                                      (meth.__name__))
        return meth(self, *args, **kwargs)

    return check


class MemoryClaimer(taskflow.job.Claimer):
    def claim(self, job, owner):
        job.owner = owner

    def unclaim(self, job, owner):
        job.owner = None


class MemoryCatalog(catalog.Catalog):
    def __init__(self):
        super(MemoryCatalog, self).__init__()
        self._catalogs = []
        self._closed = False
        self._lock = threading.RLock()

    def __len__(self):
        with self._lock:
            return len(self._catalogs)

    def __contains__(self, job):
        with self._lock:
            for (j, _b) in self._catalogs:
                if j == job:
                    return True
        return False

    def close(self):
        self._closed = True

    @check_not_closed
    def create_or_fetch(self, job):
        with self._lock:
            for (j, b) in self._catalogs:
                if j == job:
                    return b
            b = MemoryLogBook()
            self._catalogs.append((job, b))
            return b

    @check_not_closed
    def erase(self, job):
        with self._lock:
            self._catalogs = [(j, b) for (j, b) in self._catalogs if j != job]


class MemoryFlowDetail(logbook.FlowDetail):
    def __init__(self, book, name, task_cls=logbook.TaskDetail):
        super(MemoryFlowDetail, self).__init__(book, name)
        self._tasks = []
        self._task_cls = task_cls

    def __iter__(self):
        for t in self._tasks:
            yield t

    def __contains__(self, task_name):
        for t in self:
            if t.name == task_name:
                return True
        return False

    def __getitem__(self, task_name):
        return [t for t in self if t.name == task_name]

    def __len__(self):
        return len(self._tasks)

    def add_task(self, task_name, metadata=None):
        task_details = self._task_cls(task_name, metadata)
        self._tasks.append(task_details)
        return task_details

    def __delitem__(self, task_name):
        self._tasks = [t for t in self if t.name != task_name]


class MemoryLogBook(logbook.LogBook):
    def __init__(self):
        super(MemoryLogBook, self).__init__()
        self._flows = []
        self._flow_names = set()
        self._closed = False

    @check_not_closed
    def add_flow(self, flow_name):
        if flow_name in self._flow_names:
            raise exc.AlreadyExists()
        f = MemoryFlowDetail(self, flow_name)
        self._flows.append(f)
        self._flow_names.add(flow_name)
        return f

    @check_not_closed
    def __getitem__(self, flow_name):
        if flow_name not in self._flow_names:
            raise exc.NotFound()
        for w in self._flows:
            if w.name == flow_name:
                return w

    @check_not_closed
    def __iter__(self):
        for w in self._flows:
            yield w

    def close(self):
        self._closed = True

    @check_not_closed
    def __contains__(self, flow_name):
        if flow_name not in self._flow_names:
            return False
        return True

    def __delitem__(self, flow_name):
        w = self[flow_name]
        self._flow_names.remove(flow_name)
        self._flows.remove(w)

    def __len__(self):
        return len(self._flows)


class MemoryJobBoard(jobboard.JobBoard):
    def __init__(self):
        super(MemoryJobBoard, self).__init__()
        self._event = threading.Event()
        # Locking to ensure that if there are multiple
        # users posting to the backing board that we only
        # have 1 writer modifying it at a time, but we can
        # have X readers.
        self._lock = utils.ReaderWriterLock()
        self._board = []
        self._closed = False

    def close(self):
        self._closed = True

    def _select_posts(self, date_functor):
        for (d, j) in self._board:
            if date_functor(d):
                yield j

    def repost(self, job):
        # Let people know a job is here
        self._notify_posted(job)
        self._event.set()
        # And now that they are notified, reset for another posting.
        self._event.clear()

    @check_not_closed
    def post(self, job):
        with self._lock.acquire(read=False):
            self._board.append((datetime.utcnow(), job))
        # Ensure the job tracks that we posted it
        job.posted_on.append(weakref.proxy(self))
        # Let people know a job is here
        self._notify_posted(job)
        self._event.set()
        # And now that they are notified, reset for another posting.
        self._event.clear()

    @check_not_closed
    def posted_before(self, date_posted=None):
        date_functor = lambda d: True
        if date_posted is not None:
            date_functor = lambda d: d < date_posted

        with self._lock.acquire(read=True):
            return [j for j in self._select_posts(date_functor)]

    @check_not_closed
    def erase(self, job):
        with self._lock.acquire(read=False):
            # Ensure that we even have said job in the first place.
            exists = False
            for (d, j) in self._board:
                if j == job:
                    exists = True
                    break
            if not exists:
                raise exc.JobNotFound()
            if job.state not in (states.SUCCESS, states.FAILURE):
                raise exc.InvalidStateException("Can not delete a job in "
                                                "state %s" % (job.state))
            self._board = [(d, j) for (d, j) in self._board if j != job]
            self._notify_erased(job)

    @check_not_closed
    def posted_after(self, date_posted=None):
        date_functor = lambda d: True
        if date_posted is not None:
            date_functor = lambda d: d >= date_posted

        with self._lock.acquire(read=True):
            return [j for j in self._select_posts(date_functor)]

    @check_not_closed
    def await(self, timeout=None):
        self._event.wait(timeout)
