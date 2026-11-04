"""Precompute XYZ gather indices, and write into contiguous in-memory storage."""
import numpy as np


class Projection:
    def __init__(self, lut, valid):
        if lut.shape != (4, 200, 200, 4) or valid.shape != (4, 200, 200):
            raise ValueError('LUT shape mismatch')
        # C flatten of decoder [X,Y,Z,C] has Z fastest, then Y, then X.
        xyz = lut.transpose(2, 1, 0, 3).reshape(-1, 4)
        self.dest = np.flatnonzero(valid.transpose(2, 1, 0).reshape(-1))
        coordinates = xyz[self.dest].astype(np.intp)
        self.cam, self.u, self.v = coordinates[:, 0], coordinates[:, 1], coordinates[:, 2]
        if np.any((self.cam < 0) | (self.cam >= 6) | (self.u < 0) |
                  (self.u >= 176) | (self.v < 0) | (self.v >= 64)):
            raise ValueError('LUT coordinates out of bounds')

    def into(self, feature, target, group):
        if (feature.shape != (6,64,176,64) or feature.dtype != np.float32
                or not np.isfinite(feature).all()):
            raise ValueError('extractor output contract mismatch')
        if (target.shape != (1,200,200,4,256) or target.dtype != np.float32
                or not target.flags.c_contiguous or group not in range(4)):
            raise ValueError('decoder target contract mismatch')
        # Bounded gather scratch: 4096 * 64 floats = 1 MiB; no 30 MiB copy.
        flat = target.reshape(-1,256)
        for start in range(0,len(self.dest),4096):
            sl = slice(start,start+4096)
            flat[self.dest[sl],group*64:(group+1)*64] = feature[self.cam[sl],self.v[sl],self.u[sl],:]
