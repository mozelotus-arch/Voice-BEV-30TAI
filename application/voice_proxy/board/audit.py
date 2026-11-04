"""Bounded local diagnostics, without recorded PCM or raw wire payloads."""
import json
import logging
from logging.handlers import RotatingFileHandler
import time
import uuid


class Audit:
    def __init__(self, path):
        self.logger = logging.getLogger('audit.' + uuid.uuid4().hex)
        self.logger.propagate = False
        self.logger.setLevel(logging.INFO)
        self.handler = RotatingFileHandler(str(path), maxBytes=2*1024*1024,
                                          backupCount=3, encoding='utf-8')
        self.logger.addHandler(self.handler)

    def event(self, kind, value):
        self.logger.info(json.dumps(dict(time=time.time(), event=kind, value=value),
                                    ensure_ascii=False, default=str))

    def close(self):
        self.handler.close()
        self.logger.removeHandler(self.handler)
