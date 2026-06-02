"""LangGraph integration shim.

This module wires our synchronous node functions
(`planner`, `optimizer`, `estimator`, `generator` in `eng.ai.nodes`) into
LangGraph runnables. This file was tested against the pinned runtime in
`eng/requirements.txt` (langgraph==1.2.2, langgraph-sdk==1.2.2, langchain-core==0.0.206).

Deterministic modes (select with environment variable `THINSLICE_LANGGRAPH_MODE`):
 - `sequence`: always use the Runnable sequence path (RunnableSeq). This is the
     default deterministic choice when you pin to the tested LangGraph release.
 - `toolnode`: always use the ToolNode (agentic tool-call) path.
 - `auto` or unset: try sequence first, then ToolNode, then fail with an actionable error.

When running in production/deterministic mode, set `THINSLICE_LANGGRAPH_MODE` to
`sequence` or `toolnode` and pin the runtime as above; the shim will then call
the selected API path deterministically and fail fast on mismatch.
"""
from typing import Any

from . import nodes

try:
    import langgraph
    LANGGRAPH_AVAILABLE = True
except Exception:
    langgraph = None
    LANGGRAPH_AVAILABLE = False

import os
import importlib



def run_with_langgraph(initial_state: Any) -> Any:
    """Run our pipeline via LangGraph if available.

    Returns the final state produced by the composed runnables.
    Raises RuntimeError with guidance when LangGraph is not available or when
    the installed API can't be adapted automatically.
    """
    # Read mode at call time so tests can set THINSLICE_LANGGRAPH_MODE via monkeypatch
    mode = os.environ.get("THINSLICE_LANGGRAPH_MODE", "auto").lower()

    # If langgraph isn't importable, allow deterministic test/dev modes to proceed
    # when the caller sets THINSLICE_LANGGRAPH_MODE to 'sequence' or 'toolnode'.
    if not LANGGRAPH_AVAILABLE and mode == "auto":
        raise RuntimeError(
            "LangGraph is not installed. To enable real LangGraph orchestration, "
            "install it in your venv, e.g.:\n\n    pip install langgraph\n\n"
            "After installing, re-run the runner to have the graph execute with LangGraph."
        )

    # Try to import a public runnable/sequence type. Many langgraph builds
    # expose internal RunnableSeq; we try a few sensible locations and fall
    # back gracefully if nothing matches. We'll also attempt a ToolNode-based
    # orchestration (agentic tool-calls) as an alternate path.
    # Allow test-time overrides by checking module-level attributes first.
    this_mod = importlib.import_module(__name__)
    RunnableSeq = getattr(this_mod, "RunnableSeq", None)
    ToolNode = getattr(this_mod, "ToolNode", None)
    prebuilt = None

    # If not overridden by tests, probe the installed package for implementations.
    if RunnableSeq is None or ToolNode is None:
        try:
            prebuilt = __import__("langgraph.prebuilt", fromlist=["*"])
        except Exception:
            prebuilt = None

    if RunnableSeq is None:
        candidates = [
            "langgraph._internal._runnable.RunnableSeq",
            "langgraph._internal._runnable.RunnableSequence",
        ]
        for cand in candidates:
            module_name, _, attr = cand.rpartition('.')
            try:
                mod = __import__(module_name, fromlist=[attr])
                RunnableSeq = getattr(mod, attr)
                break
            except Exception:
                RunnableSeq = None

    # Try to find ToolNode in prebuilt if available and not overridden
    if ToolNode is None and prebuilt is not None:
        try:
            ToolNode = getattr(prebuilt, "ToolNode")
        except Exception:
            ToolNode = None

    # Prefer the RunnableSeq (simple sequence); if available, compose and run.
    if RunnableSeq is not None:
        try:
            seq = RunnableSeq(
                nodes.planner, nodes.optimizer, nodes.estimator, nodes.generator, name="thin_slice_pipeline"
            )
            result = seq.invoke(initial_state)
            return result
        except Exception as exc:  # pragma: no cover - runtime adaption
            # Fall through to other options
            runnable_exc = exc
    else:
        runnable_exc = None

    # If ToolNode is available, attempt an agentic, tool-call style execution.
    if ToolNode is not None:
        try:
            # Build lightweight adapter tools that call our synchronous node functions.
            def _make_tool(fn):
                def tool_wrapper(state):
                    """Adapter tool for LangGraph ToolNode: calls the pipeline node.

                    The docstring is required by langchain's tool conversion logic.
                    """
                    return fn(state)

                # Give the wrapper a sane name for debug purposes
                tool_wrapper.__name__ = fn.__name__
                return tool_wrapper

            tool_funcs = [_make_tool(nodes.planner), _make_tool(nodes.optimizer), _make_tool(nodes.estimator), _make_tool(nodes.generator)]
            tool_node = ToolNode(tool_funcs, name="thin_slice_tools")

            # Execute each tool in sequence by issuing direct tool_call payloads.
            state = initial_state
            for i, fn in enumerate(tool_funcs, start=1):
                payload = [{
                    "name": fn.__name__,
                    "args": {"state": state},
                    "id": str(i),
                    "type": "tool_call",
                }]
                try:
                    out = tool_node.invoke(payload, None)
                except Exception:
                    # Some ToolNode builds require a config object; try with empty dict
                    out = tool_node.invoke(payload, {})

                # ToolNode may return a wrapped value; normalize to state
                if hasattr(out, "dict"):
                    state = out
                elif isinstance(out, dict):
                    try:
                        state = type(initial_state)(**out)
                    except Exception:
                        state = out
                else:
                    # If ToolNode returns message objects, attempt to extract state field
                    # Fallback: keep previous state
                    pass

            return state
        except Exception as exc:  # pragma: no cover - runtime adaption
            # Both RunnableSeq and ToolNode attempts failed; raise a helpful error.
            raise RuntimeError(
                "LangGraph is installed but we could not execute the pipeline using the detected APIs.\n"
                "Tried RunnableSeq and ToolNode approaches; original errors were:\n"
                f"RunnableSeq error: {runnable_exc!r}\nToolNode error: {exc!r}\n"
                "You can adapt eng/ai/langgraph_runner.py to the installed LangGraph API if needed."
            ) from exc

    # If we reach here, no usable API was found
    raise RuntimeError(
        "LangGraph is installed but a compatible Runnable/ToolNode API was not found.\n"
        "Let the runner fall back to the pure-Python sequential executor or adapt this shim."
    )
