#!/usr/bin/env python
"""Built-in NVIDIA GPU stresser.

Drives high utilisation by running a tight ``matmul`` loop in a worker
subprocess per device.  Prefers PyTorch (already CUDA-aware) and falls
back to CuPy if PyTorch isn't present.  Workers receive a stop signal
via ``multiprocessing.Event`` and are torn down with the same
graduated approach as :class:`BuiltinStresser`.
"""

from __future__ import annotations

import logging
import os
from multiprocessing import Event, Process
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from multiprocessing.synchronize import Event as EventType


def _backend_available() -> str | None:
    """Return the backend identifier we can use, or None."""
    try:
        import torch  # pyright: ignore[reportMissingImports]

        if torch.cuda.is_available() and torch.cuda.device_count() > 0:
            return "torch"
    except ImportError:
        pass
    try:
        import cupy  # noqa: F401  # pyright: ignore[reportMissingImports]

        return "cupy"
    except ImportError:
        pass
    return None


def _device_count(backend: str) -> int:
    if backend == "torch":
        import torch  # pyright: ignore[reportMissingImports]

        return torch.cuda.device_count()
    if backend == "cupy":
        import cupy  # pyright: ignore[reportMissingImports]

        return cupy.cuda.runtime.getDeviceCount()
    return 0


def _worker_torch(device_index: int, stop_event: EventType, matrix_size: int) -> None:
    # CUDA_VISIBLE_DEVICES has to be set BEFORE torch imports cuda
    os.environ["CUDA_VISIBLE_DEVICES"] = str(device_index)
    import torch  # pyright: ignore[reportMissingImports]

    device = torch.device("cuda:0")
    try:
        a = torch.randn(matrix_size, matrix_size, device=device, dtype=torch.float32)
        b = torch.randn(matrix_size, matrix_size, device=device, dtype=torch.float32)
        c = torch.empty_like(a)
        # Warm-up + sync once
        torch.matmul(a, b, out=c)
        torch.cuda.synchronize(device)
        while not stop_event.is_set():
            for _ in range(32):
                torch.matmul(a, b, out=c)
                torch.matmul(c, a, out=b)
            torch.cuda.synchronize(device)
    except Exception:
        logging.exception("GPU stress worker on device %d crashed", device_index)


def _worker_cupy(device_index: int, stop_event: EventType, matrix_size: int) -> None:
    import cupy  # pyright: ignore[reportMissingImports]

    try:
        with cupy.cuda.Device(device_index):
            a = cupy.random.randn(matrix_size, matrix_size, dtype=cupy.float32)
            b = cupy.random.randn(matrix_size, matrix_size, dtype=cupy.float32)
            cupy.cuda.Stream.null.synchronize()
            while not stop_event.is_set():
                for _ in range(32):
                    cupy.matmul(a, b, out=a)
                    cupy.matmul(b, a, out=b)
                cupy.cuda.Stream.null.synchronize()
    except Exception:
        logging.exception("GPU stress worker on device %d crashed", device_index)


class GpuStresser:
    """Spawns one worker per CUDA device to drive utilisation to ~100%."""

    def __init__(self) -> None:
        self._stop_event: EventType | None = None
        self._workers: list[Process] = []
        self._backend = _backend_available()

    @property
    def is_available(self) -> bool:
        return self._backend is not None and _device_count(self._backend) > 0

    @property
    def backend(self) -> str | None:
        return self._backend

    def device_count(self) -> int:
        if self._backend is None:
            return 0
        try:
            return _device_count(self._backend)
        except Exception:
            return 0

    def start(self, matrix_size: int = 4096) -> None:
        if not self.is_available:
            logging.warning("GPU stresser requested but no CUDA backend available")
            return

        worker_fn = _worker_torch if self._backend == "torch" else _worker_cupy
        n_devices = self.device_count()

        self.stop()
        self._stop_event = Event()
        try:
            for idx in range(n_devices):
                p = Process(
                    target=worker_fn,
                    args=(idx, self._stop_event, matrix_size),
                    daemon=True,
                )
                p.start()
                self._workers.append(p)
        except OSError:
            logging.exception(
                "Failed to start GPU stress workers; cleaning up %d already-started",
                len(self._workers),
            )
            self.stop()
            raise
        logging.info(
            "GPU stresser started %d workers (backend: %s, matmul %dx%d)",
            n_devices,
            self._backend,
            matrix_size,
            matrix_size,
        )

    def stop(self, timeout: int = 3) -> None:
        if not self._workers:
            return
        if self._stop_event is not None:
            self._stop_event.set()
        for p in self._workers:
            p.join(timeout=timeout)
        for p in self._workers:
            if p.is_alive():
                logging.debug("Terminating GPU stress straggler %s", p.pid)
                p.terminate()
                p.join(timeout=1)
        for p in self._workers:
            if p.is_alive():
                logging.debug("Killing GPU stress straggler %s", p.pid)
                p.kill()
        for p in self._workers:
            p.join(timeout=1)
        self._workers.clear()
        logging.info("GPU stresser stopped")

    def is_running(self) -> bool:
        return any(p.is_alive() for p in self._workers)
