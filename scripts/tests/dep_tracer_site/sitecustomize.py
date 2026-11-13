"""Starts the dependency tracer in a Python child of a traced test, then runs the next sitecustomize."""
import importlib.machinery
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _start_tracer():
    if os.environ.get("CORE_DEP_TRACER") != "1" or not os.environ.get("CORE_DEP_TRACER_CHILD_DIR"):
        return
    if os.environ.get("CORE_DEP_TRACER_NO_SUBPROCESS"):
        return
    module = sys.modules.get("dep_tracer")
    if module is None:
        path = os.path.join(os.path.dirname(_HERE), "dep_tracer.py")
        spec = importlib.util.spec_from_file_location("dep_tracer", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["dep_tracer"] = module
        spec.loader.exec_module(module)
    module.start_child()


def _run_next_sitecustomize():
    rest = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != _HERE]
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", rest)
    if spec is not None and spec.loader is not None and spec.origin != __file__:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)


try:
    _start_tracer()
except Exception:
    pass
_run_next_sitecustomize()
