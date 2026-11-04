"""Cooperative cancellation around non-interruptible SDK calls."""
class InteractionCancelled(RuntimeError):
    pass


class GuardedCall:
    def __init__(self, target, methods, check, lock=None):
        self.target, self.methods, self.check, self.lock = target, methods, check, lock

    def __getattr__(self, name):
        method = getattr(self.target, name)
        if name not in self.methods:
            return method
        def invoke(*args, **kwargs):
            # Only short simulated task dispatch uses the lock. Never hold the
            # control lock across an NPU call: stop must remain responsive.
            if self.lock is not None:
                with self.lock:
                    self.check()
                    return method(*args, **kwargs)
            self.check()
            result = method(*args, **kwargs)
            self.check()
            return result
        return invoke


def install_guards(service, runtime, bridge):
    for name, methods in (('asr', {'transcribe'}), ('tts', {'synthesize'}), ('sink', {'write'})):
        setattr(runtime, name, GuardedCall(getattr(runtime, name), methods, service.check_interaction))
    runtime.session.nlu = GuardedCall(runtime.session.nlu, {'understand'}, service.check_interaction)
    tasks = GuardedCall(bridge.tasks, {'apply', 'query'}, service.check_interaction, service.lock)
    bridge.tasks = tasks
    runtime.session.dialogue.executor = tasks
    bridge.text_dialogue.executor = tasks
