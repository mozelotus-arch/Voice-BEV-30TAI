"""Exclusive model residence plus bounded serial dispatch. No NPU resets/kills."""
from concurrent.futures import Future
from dataclasses import dataclass
import math
import json
import os
from pathlib import Path
import threading
import time

# Strong process roots survive callers dropping a failed scheduler. Native DMA
# references and the lease must never depend on a daemon worker's stack lifetime.
NATIVE_QUARANTINE_ROOTS=[]

def retain_quarantined_scheduler(scheduler,backend,lease):
    NATIVE_QUARANTINE_ROOTS.append((scheduler,backend,lease))
    if scheduler.hardware:
        keeper=threading.Thread(target=threading.Event().wait,name='native-quarantine-retain-until-platform-recovery',daemon=False)
        scheduler._quarantine_keeper=keeper
        keeper.start()


class RequestCancelled(TimeoutError):
    """A stage boundary cancellation with no native operation in flight."""

class QuiescentInitializationError(RuntimeError):
    """Factory failed, but its child acknowledged close and exited successfully."""

class QuiescentOperationError(RuntimeError):
    """Adapter proves no native work remains after this request failed."""


class NpuLease:
    def __init__(self, path):
        self.path, self.file = Path(path), None
        self.marker = self.path.with_name(self.path.name + '.active.json')
        self.quarantined = False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = self.path.open('a+b')
        try:
            if f.seek(0, 2) == 0:
                f.write(b'0'); f.flush()
            f.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.marker.exists():
                raise RuntimeError('previous_NPU_owner_quiescence_unconfirmed')
            with self.marker.open('x', encoding='utf-8') as marker:
                json.dump(dict(pid=os.getpid(), state='active', recovery_requires_quiescence=True), marker)
        except BaseException:
            f.close()
            raise RuntimeError('NPU_model_owner_already_running') from None
        self.file = f
        return self

    def quarantine(self, reason):
        self.quarantined = True
        self.marker.write_text(json.dumps(dict(pid=os.getpid(), state='quarantined',
            reason=str(reason)[:160], recovery_requires_quiescence=True)), encoding='utf-8')

    def __exit__(self, *unused):
        if self.file is not None:
            if not self.quarantined:
                self.marker.unlink()
            if os.name == 'nt':
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            self.file.close()
            self.file = None


@dataclass(frozen=True)
class Budget:
    resident_bytes: int
    scratch_peak_bytes: int
    measured_max_call_s: float
    measured: bool
    load_timeout_s: float = None

    def validate(self, limit, hardware):
        if any(type(v) is not int or v < 0 for v in (self.resident_bytes, self.scratch_peak_bytes)):
            raise ValueError('memory_budget_required')
        if self.resident_bytes + self.scratch_peak_bytes > limit:
            raise ValueError('memory_budget_exceeded')
        if not math.isfinite(self.measured_max_call_s) or self.measured_max_call_s <= 0:
            raise ValueError('latency_budget_required')
        if hardware and self.measured is not True:
            raise ValueError('board_memory_and_latency_measurements_required')
        if self.load_timeout_s is not None and (type(self.load_timeout_s) not in (int,float)
                or not math.isfinite(self.load_timeout_s) or self.load_timeout_s <= 0):
            raise ValueError('positive_load_timeout_required')


class SerialModelScheduler:
    """Exactly one resident backend at a time, including initialization/close.

    Factories and calls execute in one dedicated worker. Factories must expose
    run(op, payload) and close(), whose return proves resources are quiescent.
    A native call timeout quarantines the worker until it returns; cancelling a
    Future never grants another worker access to the accelerator.
    """
    def __init__(self, factories, budgets, memory_limit_bytes, lock_path, *, hardware=False,
                 clock=time.monotonic, max_pending=4, supervised_capacity_validation=False):
        if set(factories) != set(budgets) or type(memory_limit_bytes) is not int or memory_limit_bytes <= 0:
            raise ValueError('complete_resource_budget_required')
        for budget in budgets.values():
            budget.validate(memory_limit_bytes, hardware and not supervised_capacity_validation)
        self.factories, self.budgets, self.clock = factories, budgets, clock
        self.hardware=hardware
        self.lock_path, self.max_pending = lock_path, max_pending
        self.cv = threading.Condition()
        self.pending = []
        self.generation = 0
        self.closed = False
        self.failed = None
        self.active = None
        self.resident = None
        self.pinned = None
        self.sequence = 0
        self.owner_thread_id = None
        # Process exit must not abandon a native teardown or its child join.
        self.thread = threading.Thread(target=self._work, name='single-model-scheduler', daemon=False)
        self.thread.start()

    def submit(self, model, op, payload, deadline, *, key=None, priority=10):
        with self.cv:
            if self.closed or self.failed:
                raise RuntimeError('model_scheduler_unavailable')
            if self.pinned is not None and model != self.pinned:
                raise RuntimeError('model_residence_reserved_for_validation:' + self.pinned)
            if model not in self.factories or not math.isfinite(deadline) or deadline <= self.clock():
                raise ValueError('model_or_deadline_invalid')
            # Replace queued perception with latest; never queue an old frame train.
            if key is not None:
                for old in list(self.pending):
                    if old['key'] == key:
                        old['future'].cancel()
                        self.pending.remove(old)
            if len(self.pending) >= self.max_pending:
                raise RuntimeError('model_queue_full')
            self.sequence += 1
            future = Future()
            item = dict(model=model, op=op, payload=payload, deadline=deadline, key=key,
                        priority=priority, sequence=self.sequence, generation=self.generation, future=future)
            self.pending.append(item)
            self.cv.notify()
            return future

    def pin(self, model):
        with self.cv:
            if model not in self.factories or self.pinned not in (None, model):
                raise RuntimeError('incompatible_model_residence_reservation')
            if any(item['model'] != model for item in self.pending) or (self.active and self.active['model'] != model):
                raise RuntimeError('drain_other_model_before_reservation')
            self.pinned = model

    def unpin(self, model):
        with self.cv:
            if self.pinned == model: self.pinned = None

    def cancel(self):
        with self.cv:
            self.generation += 1
            for item in self.pending:
                item['future'].cancel()
            self.pending.clear()
            self.cv.notify()

    def status(self):
        with self.cv:
            active = self.active
            limit = None
            if active:
                budget=self.budgets[active['model']]
                limit=budget.load_timeout_s if active.get('loading') and budget.load_timeout_s is not None else budget.measured_max_call_s
            overrun = bool(active and self.clock() - active.get('phase_started',active['started']) > limit)
            return dict(active=None if active is None else active['model'], resident=self.resident,
                        queued=len(self.pending), generation=self.generation, failed=self.failed,
                        overrun=overrun, replacement_owner_allowed=False,
                        hardware_concurrency=1)

    def assert_owner(self):
        if threading.get_ident() != self.owner_thread_id or self.failed or self.active is None:
            raise RuntimeError('accelerator_call_outside_serial_owner')

    def _work(self):
        backend = None
        lease = NpuLease(self.lock_path)
        uncertain = False
        try:
            lease.__enter__()
            self.owner_thread_id = threading.get_ident()
            while True:
                with self.cv:
                    self.cv.wait_for(lambda: self.pending or self.closed)
                    if self.closed:
                        break
                    item = min(self.pending, key=lambda x: (x['priority'], x['sequence']))
                    self.pending.remove(item)
                    if item['deadline'] <= self.clock() or not item['future'].set_running_or_notify_cancel():
                        if not item['future'].done():
                            item['future'].set_exception(RequestCancelled('model_request_expired_before_start'))
                        continue
                    item['started'] = self.clock()
                    call_started = item['started']
                    self.active = item
                try:
                    if self.resident != item['model']:
                        item['loading']=True
                        if backend is not None:
                            uncertain = True
                            backend.close()  # wait for confirmed unload before loading next model
                            backend = None
                            uncertain = False
                        self.resident = None
                        uncertain = True  # includes partially constructed native backends
                        backend = self.factories[item['model']]()
                        uncertain = False
                        self.resident = item['model']
                        call_started = self.clock()
                        load_limit = self.budgets[item['model']].load_timeout_s
                        if load_limit is not None and call_started-item['started'] > load_limit:
                            raise RequestCancelled('model_load_timeout')
                    item['loading']=False
                    item['phase_started']=call_started
                    with self.cv:
                        cancelled = item['generation'] != self.generation or self.closed
                    if cancelled or self.clock() >= item['deadline']:
                        raise RequestCancelled('cancelled_or_expired_after_model_load')
                    uncertain = True
                    result = backend.run(item['op'], item['payload'])
                    uncertain = False  # returned output must be synchronized by the adapter
                    elapsed = self.clock() - (call_started if self.budgets[item['model']].load_timeout_s is not None else item['started'])
                    with self.cv:
                        stale = item['generation'] != self.generation or self.clock() >= item['deadline'] or self.closed
                    if elapsed > self.budgets[item['model']].measured_max_call_s:
                        raise RequestCancelled('model_latency_budget_overrun')
                    if stale:
                        item['future'].set_exception(RequestCancelled('model_result_cancelled_or_stale'))
                    else:
                        item['future'].set_result(result)
                except RequestCancelled as exc:
                    uncertain = False
                    if not item['future'].done():
                        item['future'].set_exception(exc)
                except QuiescentOperationError as exc:
                    if backend is None or item.get('loading'):
                        if not item['future'].done():item['future'].set_exception(exc)
                        raise  # A request proof cannot attest a failed factory.
                    uncertain = False
                    if not item['future'].done():item['future'].set_exception(exc)
                    # Retain models for a later explicit request. Shutdown
                    # still requires the child's close receipt and clean exit.
                except QuiescentInitializationError as exc:
                    if backend is not None or not item.get('loading'):
                        if not item['future'].done():item['future'].set_exception(exc)
                        raise  # Only a failed factory can supply this proof.
                    uncertain=False
                    if not item['future'].done():item['future'].set_exception(exc)
                    raise  # Fail this scheduler normally and release the idle lease.
                except BaseException as exc:
                    if not item['future'].done():
                        item['future'].set_exception(exc)
                    # Any uncertain native failure quarantines this owner. No new job.
                    raise
                finally:
                    with self.cv:
                        self.active = None
        except BaseException as exc:
            with self.cv:
                self.failed = str(exc)[:180]
                for item in self.pending:
                    item['future'].cancel()
                self.pending.clear()
        finally:
            if uncertain:
                # Do NOT invoke teardown after an uncertain native exception:
                # even a destructor could free DMA buffers. Hold both objects.
                self._quarantined_backend = backend
                self._quarantined_lease = lease
                if lease.file is not None:
                    lease.quarantine(self.failed or 'native_state_uncertain')
                retain_quarantined_scheduler(self,backend,lease)
                return
            if backend is not None:
                try:
                    backend.close()
                except BaseException as exc:
                    # Hold the process-owned lease if teardown cannot establish
                    # quiescence. The operator must not start a replacement.
                    with self.cv:
                        self.failed = 'backend_quiescence_unconfirmed:' + str(exc)[:120]
                        self._quarantined_lease = lease
                        self._quarantined_backend = backend
                        lease.quarantine(self.failed)
                        retain_quarantined_scheduler(self,backend,lease)
                    return
            self.resident = None
            lease.__exit__()

    def close(self, timeout=None):
        with self.cv:
            self.closed = True
            self.cancel()
            self.cv.notify()
        self.thread.join(timeout)
        return dict(quiescent=not self.thread.is_alive() and not hasattr(self, '_quarantined_lease'),
                    failed=self.failed)
