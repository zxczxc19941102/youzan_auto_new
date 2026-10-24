<<<<<<< HEAD
# -*- coding: utf-8 -*-
"""Tkinter 自动化任务共享的线程池、日志和取消运行时。"""
import contextlib
import time
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import thread as futures_thread


class TaskCancelled(BaseException):
    """协作式取消信号，避免被业务代码的 except Exception 吞掉。"""


class DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """Python 3.12 线程池变体，保持 ThreadPoolExecutor 的有界调度并使用 daemon。"""

    def _adjust_thread_count(self):
        if self._idle_semaphore.acquire(timeout=0):
            return

        def wake_executor(_reference, work_queue=self._work_queue):
            work_queue.put(None)

        count = len(self._threads)
        if count >= self._max_workers:
            return
        name = '%s_%d' % (self._thread_name_prefix or self, count)
        # CPython 3.14 changed ThreadPoolExecutor internals: _worker now
        # receives a prepared WorkerContext and ThreadPoolExecutor no longer
        # exposes _initializer/_initargs. Keep the daemon-thread behavior
        # while supporting both the new and old private APIs.
        if hasattr(self, '_create_worker_context'):
            worker_args = (
                weakref.ref(self, wake_executor),
                self._create_worker_context(),
                self._work_queue,
            )
        else:
            worker_args = (
                weakref.ref(self, wake_executor),
                self._work_queue,
                getattr(self, '_initializer', None),
                getattr(self, '_initargs', ()),
            )
        worker = threading.Thread(
            name=name,
            target=futures_thread._worker,
            args=worker_args,
            daemon=True,
        )
        worker.start()
        self._threads.add(worker)
        futures_thread._threads_queues[worker] = self._work_queue


class TaskRuntime:
    """一轮自动化任务的日志、并发状态、计时和协作式取消管理器。"""

    def __init__(self, max_workers, stop_event, log_queue, sink=None):
        self.max_workers = int(max_workers)
        self.stop_event = stop_event
        self.log_queue = log_queue
        self.sink = sink          # 实时写盘回调（LOG/*.txt）
        self.executor = self._new_executor()
        self.futures = []
        self._child_executors = []
        self._lock = threading.Lock()
        self._local = threading.local()
        self.active_count = 0
        self.child_active = 0     # 阶段性子池正在执行的线程数
        self.success_count = 0
        self.failed_count = 0
        self.interrupted_count = 0
        self.started_at = time.perf_counter()
        self.stop_started_at = None
        self._finished = False

    def _new_executor(self):
        return DaemonThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix='YouzanWorker',
        )

    def counts(self):
        """活跃数：批量阶段按子池统计，且不超过并发上限（父任务此时在等待）。"""
        with self._lock:
            active = self.child_active or self.active_count
            return min(active, self.max_workers), self.max_workers

    def attach_executor(self, executor):
        """登记阶段性子线程池，终止时一并取消排队任务。"""
        with self._lock:
            self._child_executors.append(executor)

    def begin_task(self, label, emit=True):
        """阶段性子线程池里的任务开始：单独计数，避免叠加父任务出现 6/5。"""
        with self._lock:
            self.child_active += 1
        if emit:
            self.emit('任务 %s 开始' % label)

    def end_task(self, label, status, started, emit=True):
        elapsed = time.perf_counter() - started
        if emit and status != '成功':
            self.emit('任务 %s 结束，总耗时 %.2f 秒，结果:%s' % (
                label, elapsed, status
            ))
        with self._lock:
            self.child_active = max(0, self.child_active - 1)
            if status == '成功':
                self.success_count += 1
            elif status == '失败':
                self.failed_count += 1
            else:
                self.interrupted_count += 1

    def emit(self, message, kind='normal', source=None):
        active, total = self.counts()
        current = threading.current_thread()
        name = source or (
            '主控' if current is threading.main_thread() else current.name
        )
        line = '[%s][%s][活跃 %d/总 %d] %s' % (
            time.strftime('%H:%M:%S'), name, active, total, str(message)
        )
        self.log_queue.put((line, kind))
        if self.sink is not None:
            try:
                self.sink(line)
            except Exception:
                pass
        try:
            print(line, flush=True)
        except Exception:
            pass

    @contextlib.contextmanager
    def step(
        self, name, inputs='无', result='完成',
        emit_start=True, emit_success=True,
    ):
        """统一计时步骤，开始、成功、失败、终止都产生结构化日志。"""
        started = time.perf_counter()
        if emit_start:
            self.emit('▶ 开始:%s，关键入参:%s' % (name, inputs))
        outcome = {'value': result}
        try:
            yield outcome
        except TaskCancelled:
            elapsed = time.perf_counter() - started
            self.emit('%s 已终止，已执行 %.2f 秒' % (name, elapsed), 'warning')
            raise
        except BaseException as exc:
            elapsed = time.perf_counter() - started
            summary = '%s: %s' % (type(exc).__name__, str(exc)[:240])
            self.emit('%s 失败，耗时 %.2f 秒，原因:%s' % (
                name, elapsed, summary
            ), 'error')
            raise
        else:
            elapsed = time.perf_counter() - started
            if emit_success:
                self.emit('✔ 完成:%s，关键结果:%s，耗时 %.2f 秒' % (
                    name, outcome['value'], elapsed
                ), 'success')

    @contextlib.contextmanager
    def task_scope(self, label):
        """为批次中的每个账号/店铺提供单独的总耗时日志。"""
        previous = getattr(self._local, 'label', '')
        self._local.label = str(label)
        started = time.perf_counter()
        self.emit('任务 %s 开始' % label)
        state = {'result': '成功'}
        try:
            yield state
        except TaskCancelled:
            state['result'] = '已终止'
            raise
        except BaseException:
            state['result'] = '失败'
            raise
        finally:
            elapsed = time.perf_counter() - started
            self.emit('任务 %s 结束，总耗时 %.2f 秒，结果:%s' % (
                label, elapsed, state['result']
            ))
            self._local.label = previous

    def submit(self, label, function, *args, **kwargs):
        """提交有界任务并统一维护活跃数与任务耗时。"""
        label = str(label)

        def run_task():
            started = time.perf_counter()
            previous = getattr(self._local, 'label', '')
            self._local.label = label
            with self._lock:
                self.active_count += 1
            self.emit('任务 %s 开始' % label)
            status = '失败'
            try:
                if self.stop_event.is_set():
                    raise TaskCancelled()
                result = function(*args, **kwargs)
                if isinstance(result, dict):
                    ok = bool(result.get('success', True))
                    status = '成功' if ok else '失败'
                else:
                    status = '成功' if result is not False else '失败'
                return result
            except TaskCancelled:
                status = '已终止'
                raise
            except BaseException as exc:
                status = '失败'
                self.emit('任务异常：%s: %s' % (
                    type(exc).__name__, str(exc)[:240]
                ), 'error')
                raise
            finally:
                elapsed = time.perf_counter() - started
                self.emit('任务 %s 结束，总耗时 %.2f 秒，结果:%s' % (
                    label, elapsed, status
                ))
                with self._lock:
                    self.active_count = max(0, self.active_count - 1)
                    if status == '成功':
                        self.success_count += 1
                    elif status == '失败':
                        self.failed_count += 1
                    else:
                        self.interrupted_count += 1
                self._local.label = previous

        future = self.executor.submit(run_task)
        self.futures.append(future)

        def handle_cancelled(done_future):
            if done_future.cancelled():
                with self._lock:
                    self.interrupted_count += 1
                self.emit('任务 %s 已取消，尚未开始' % label, 'warning')

        future.add_done_callback(handle_cancelled)
        return future

    def idle(self):
        return bool(self.futures) and all(future.done() for future in self.futures)

    def result(self, future):
        try:
            return future.result()
        except BaseException:
            return None

    def next_phase(self):
        """阶段切换时回收旧池并新建同并发上限的线程池。"""
        if not self.idle():
            raise RuntimeError('当前阶段仍有任务运行')
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.futures = []
        self.executor = self._new_executor()

    def stop(self):
        """立即发出取消信号并取消尚未运行的 Future，不等待阻塞网络调用。"""
        if self.stop_started_at is None:
            self.stop_started_at = time.perf_counter()
        self.stop_event.set()
        cancelled = 0
        for future in tuple(self.futures):
            if future.cancel():
                cancelled += 1
        self.executor.shutdown(wait=False, cancel_futures=True)
        with self._lock:
            children = list(self._child_executors)
        for executor in children:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        return cancelled

    def finish(self, stopped=False, quiet_success=False):
        if self._finished:
            return
        self._finished = True
        elapsed = time.perf_counter() - self.started_at
        with self._lock:
            self.active_count = 0
            self.child_active = 0
            success = self.success_count
            failed = self.failed_count
            interrupted = self.interrupted_count
        if stopped:
            stop_elapsed = (
                time.perf_counter() - self.stop_started_at
                if self.stop_started_at is not None else elapsed
            )
            self.emit(
                '全部任务已终止，共中断 %d 个任务，耗时 %.2f 秒'
                % (interrupted, stop_elapsed),
                source='主控',
            )
        if not quiet_success or failed or interrupted:
            self.emit(
                '整批任务结束，总耗时 %.2f 秒，成功 %d 个，失败 %d 个，终止 %d 个'
                % (elapsed, success, failed, interrupted),
                source='主控',
            )
        self.executor.shutdown(wait=False, cancel_futures=True)
        with self._lock:
            children = list(self._child_executors)
            self._child_executors = []
        for executor in children:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        self.stop_event.clear()
=======
# -*- coding: utf-8 -*-
"""Tkinter 自动化任务共享的线程池、日志和取消运行时。"""
import contextlib
import time
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import thread as futures_thread


class TaskCancelled(BaseException):
    """协作式取消信号，避免被业务代码的 except Exception 吞掉。"""


class DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """Python 3.12 线程池变体，保持 ThreadPoolExecutor 的有界调度并使用 daemon。"""

    def _adjust_thread_count(self):
        if self._idle_semaphore.acquire(timeout=0):
            return

        def wake_executor(_reference, work_queue=self._work_queue):
            work_queue.put(None)

        count = len(self._threads)
        if count >= self._max_workers:
            return
        name = '%s_%d' % (self._thread_name_prefix or self, count)
        # CPython 3.14 changed ThreadPoolExecutor internals: _worker now
        # receives a prepared WorkerContext and ThreadPoolExecutor no longer
        # exposes _initializer/_initargs. Keep the daemon-thread behavior
        # while supporting both the new and old private APIs.
        if hasattr(self, '_create_worker_context'):
            worker_args = (
                weakref.ref(self, wake_executor),
                self._create_worker_context(),
                self._work_queue,
            )
        else:
            worker_args = (
                weakref.ref(self, wake_executor),
                self._work_queue,
                getattr(self, '_initializer', None),
                getattr(self, '_initargs', ()),
            )
        worker = threading.Thread(
            name=name,
            target=futures_thread._worker,
            args=worker_args,
            daemon=True,
        )
        worker.start()
        self._threads.add(worker)
        futures_thread._threads_queues[worker] = self._work_queue


class TaskRuntime:
    """一轮自动化任务的日志、并发状态、计时和协作式取消管理器。"""

    def __init__(self, max_workers, stop_event, log_queue, sink=None):
        self.max_workers = int(max_workers)
        self.stop_event = stop_event
        self.log_queue = log_queue
        self.sink = sink          # 实时写盘回调（LOG/*.txt）
        self.executor = self._new_executor()
        self.futures = []
        self._child_executors = []
        self._lock = threading.Lock()
        self._local = threading.local()
        self.active_count = 0
        self.child_active = 0     # 阶段性子池正在执行的线程数
        self.success_count = 0
        self.failed_count = 0
        self.interrupted_count = 0
        self.started_at = time.perf_counter()
        self.stop_started_at = None
        self._finished = False

    def _new_executor(self):
        return DaemonThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix='YouzanWorker',
        )

    def counts(self):
        """活跃数：批量阶段按子池统计，且不超过并发上限（父任务此时在等待）。"""
        with self._lock:
            active = self.child_active or self.active_count
            return min(active, self.max_workers), self.max_workers

    def attach_executor(self, executor):
        """登记阶段性子线程池，终止时一并取消排队任务。"""
        with self._lock:
            self._child_executors.append(executor)

    def begin_task(self, label, emit=True):
        """阶段性子线程池里的任务开始：单独计数，避免叠加父任务出现 6/5。"""
        with self._lock:
            self.child_active += 1
        if emit:
            self.emit('任务 %s 开始' % label)

    def end_task(self, label, status, started, emit=True):
        elapsed = time.perf_counter() - started
        if emit and status != '成功':
            self.emit('任务 %s 结束，总耗时 %.2f 秒，结果:%s' % (
                label, elapsed, status
            ))
        with self._lock:
            self.child_active = max(0, self.child_active - 1)
            if status == '成功':
                self.success_count += 1
            elif status == '失败':
                self.failed_count += 1
            else:
                self.interrupted_count += 1

    def emit(self, message, kind='normal', source=None):
        active, total = self.counts()
        current = threading.current_thread()
        name = source or (
            '主控' if current is threading.main_thread() else current.name
        )
        line = '[%s][%s][活跃 %d/总 %d] %s' % (
            time.strftime('%H:%M:%S'), name, active, total, str(message)
        )
        self.log_queue.put((line, kind))
        if self.sink is not None:
            try:
                self.sink(line)
            except Exception:
                pass
        try:
            print(line, flush=True)
        except Exception:
            pass

    @contextlib.contextmanager
    def step(
        self, name, inputs='无', result='完成',
        emit_start=True, emit_success=True,
    ):
        """统一计时步骤，开始、成功、失败、终止都产生结构化日志。"""
        started = time.perf_counter()
        if emit_start:
            self.emit('▶ 开始:%s，关键入参:%s' % (name, inputs))
        outcome = {'value': result}
        try:
            yield outcome
        except TaskCancelled:
            elapsed = time.perf_counter() - started
            self.emit('%s 已终止，已执行 %.2f 秒' % (name, elapsed), 'warning')
            raise
        except BaseException as exc:
            elapsed = time.perf_counter() - started
            summary = '%s: %s' % (type(exc).__name__, str(exc)[:240])
            self.emit('%s 失败，耗时 %.2f 秒，原因:%s' % (
                name, elapsed, summary
            ), 'error')
            raise
        else:
            elapsed = time.perf_counter() - started
            if emit_success:
                self.emit('✔ 完成:%s，关键结果:%s，耗时 %.2f 秒' % (
                    name, outcome['value'], elapsed
                ), 'success')

    @contextlib.contextmanager
    def task_scope(self, label):
        """为批次中的每个账号/店铺提供单独的总耗时日志。"""
        previous = getattr(self._local, 'label', '')
        self._local.label = str(label)
        started = time.perf_counter()
        self.emit('任务 %s 开始' % label)
        state = {'result': '成功'}
        try:
            yield state
        except TaskCancelled:
            state['result'] = '已终止'
            raise
        except BaseException:
            state['result'] = '失败'
            raise
        finally:
            elapsed = time.perf_counter() - started
            self.emit('任务 %s 结束，总耗时 %.2f 秒，结果:%s' % (
                label, elapsed, state['result']
            ))
            self._local.label = previous

    def submit(self, label, function, *args, **kwargs):
        """提交有界任务并统一维护活跃数与任务耗时。"""
        label = str(label)

        def run_task():
            started = time.perf_counter()
            previous = getattr(self._local, 'label', '')
            self._local.label = label
            with self._lock:
                self.active_count += 1
            self.emit('任务 %s 开始' % label)
            status = '失败'
            try:
                if self.stop_event.is_set():
                    raise TaskCancelled()
                result = function(*args, **kwargs)
                if isinstance(result, dict):
                    ok = bool(result.get('success', True))
                    status = '成功' if ok else '失败'
                else:
                    status = '成功' if result is not False else '失败'
                return result
            except TaskCancelled:
                status = '已终止'
                raise
            except BaseException as exc:
                status = '失败'
                self.emit('任务异常：%s: %s' % (
                    type(exc).__name__, str(exc)[:240]
                ), 'error')
                raise
            finally:
                elapsed = time.perf_counter() - started
                self.emit('任务 %s 结束，总耗时 %.2f 秒，结果:%s' % (
                    label, elapsed, status
                ))
                with self._lock:
                    self.active_count = max(0, self.active_count - 1)
                    if status == '成功':
                        self.success_count += 1
                    elif status == '失败':
                        self.failed_count += 1
                    else:
                        self.interrupted_count += 1
                self._local.label = previous

        future = self.executor.submit(run_task)
        self.futures.append(future)

        def handle_cancelled(done_future):
            if done_future.cancelled():
                with self._lock:
                    self.interrupted_count += 1
                self.emit('任务 %s 已取消，尚未开始' % label, 'warning')

        future.add_done_callback(handle_cancelled)
        return future

    def idle(self):
        return bool(self.futures) and all(future.done() for future in self.futures)

    def result(self, future):
        try:
            return future.result()
        except BaseException:
            return None

    def next_phase(self):
        """阶段切换时回收旧池并新建同并发上限的线程池。"""
        if not self.idle():
            raise RuntimeError('当前阶段仍有任务运行')
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.futures = []
        self.executor = self._new_executor()

    def stop(self):
        """立即发出取消信号并取消尚未运行的 Future，不等待阻塞网络调用。"""
        if self.stop_started_at is None:
            self.stop_started_at = time.perf_counter()
        self.stop_event.set()
        cancelled = 0
        for future in tuple(self.futures):
            if future.cancel():
                cancelled += 1
        self.executor.shutdown(wait=False, cancel_futures=True)
        with self._lock:
            children = list(self._child_executors)
        for executor in children:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        return cancelled

    def finish(self, stopped=False, quiet_success=False):
        if self._finished:
            return
        self._finished = True
        elapsed = time.perf_counter() - self.started_at
        with self._lock:
            self.active_count = 0
            self.child_active = 0
            success = self.success_count
            failed = self.failed_count
            interrupted = self.interrupted_count
        if stopped:
            stop_elapsed = (
                time.perf_counter() - self.stop_started_at
                if self.stop_started_at is not None else elapsed
            )
            self.emit(
                '全部任务已终止，共中断 %d 个任务，耗时 %.2f 秒'
                % (interrupted, stop_elapsed),
                source='主控',
            )
        if not quiet_success or failed or interrupted:
            self.emit(
                '整批任务结束，总耗时 %.2f 秒，成功 %d 个，失败 %d 个，终止 %d 个'
                % (elapsed, success, failed, interrupted),
                source='主控',
            )
        self.executor.shutdown(wait=False, cancel_futures=True)
        with self._lock:
            children = list(self._child_executors)
            self._child_executors = []
        for executor in children:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        self.stop_event.clear()
>>>>>>> b94e3e2 (修复Python 3.14登录并持久化本地配置)
